"""Admin-only controls and uncached single-stock 70-session benchmark."""
import contextvars
import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import spot_chip as spot
import warrant_ai_tools as tools

_BENCHMARK_LOCK = threading.Lock()


def parse(question):
    q = re.sub(r'\s+', '', question)
    if not re.search(r'現股|分點|籌碼抓取', q):
        return None
    if re.search(r'恢復自動|重設自動|解除.*(?:固定|鎖定)|重新.*12條', q):
        return {'action':'set','parallel':12}
    setting = re.search(r'(?:改成|改為|調整為|調成|調到|設定成|設定為|設成|設為|用|開到|開)(\d+)(?:條|線|個連線)', q)
    benchmark = bool(re.search(r'測速|速度測試|測試.*速度|速度.*測試|抓.*多快|測試.*70日|70日.*測試', q))
    if benchmark:
        code = re.search(r'(?<!\d)(\d{4})(?!\d)', q)
        return {'action':'benchmark','code':code.group(1) if code else '',
                'parallel':int(setting.group(1)) if setting else None}
    if setting:
        return {'action':'set','parallel':int(setting.group(1))}
    if re.search(r'設定|幾條|多少條|連線數|抓取狀態', q):
        return {'action':'status'}
    return None


def benchmark(code, *, parallel=None, dates=None, fetch_source=None, timeout=180):
    if not _BENCHMARK_LOCK.acquire(blocking=False):
        raise ValueError('已有一個現股測速正在進行，請等它完成。')
    stock_lock = spot._stock_lock(code)
    acquired = False
    try:
        if spot.source_cooling():
            raise ValueError('現股來源正在冷卻，請稍後再測速。')
        dates = list(dates if dates is not None else spot.candidate_dates()[0])[-70:]
        if len(dates) != 70:
            raise ValueError('目前交易日資料不足 70 日，無法執行完整測速。')
        acquired = stock_lock.acquire(timeout=10)
        if not acquired:
            raise ValueError('這支股票正在背景補資料，請稍後再測速。')
        settings = spot.fetch_settings()
        workers = int(parallel or settings['parallel'])
        if not 1 <= workers <= 64:
            raise ValueError('連線數請設定為 1～64 條。')
        workers = 1 if spot.FETCHER == 'selenium' else min(workers, settings['global_limit'], 70)
        fetch_source = fetch_source or spot.open_source
        started = time.perf_counter()
        deadline = time.monotonic() + timeout
        results, guard = [], threading.Lock()
        concurrency={'active':0,'peak':0,'retry_seen':False,'health_failed':False,'setup_failed':False,'http_requests':0}
        queue = iter(reversed(dates))
        request_id = str(getattr(tools._API_REQUEST_LOCAL, 'request_id', '') or '')
        def worker():
            spot._FETCH_DEADLINE.at = deadline
            try:
                with tools.api_request_scope(request_id), fetch_source() as source_fetch:
                    def fetch(code, day):
                        try:
                            with guard:
                                concurrency['http_requests'] += 1
                            return source_fetch(code, day)
                        except Exception:
                            concurrency['retry_seen'] = True
                            time.sleep(0.2)
                            with guard:
                                concurrency['http_requests'] += 1
                            return source_fetch(code, day)
                    while time.monotonic() < deadline and not spot.source_cooling():
                        with guard:
                            day = next(queue, None)
                        if day is None:
                            break
                        queued = time.perf_counter()
                        if not spot._acquire_source(deadline, background=False):
                            break
                        waiting = time.perf_counter() - queued
                        with guard:
                            concurrency['active'] += 1
                            concurrency['peak'] = max(concurrency['peak'],concurrency['active'])
                        fetch_at = time.perf_counter()
                        http_seconds = write_seconds = 0
                        try:
                            html = fetch(code, day)
                            state, rows, detail = spot.validate_spot_snapshot(html, code, day)
                            if state in ('blocked', 'source_error'):
                                concurrency['retry_seen'] = True
                                time.sleep(0.2)
                                html = fetch(code, day)
                            http_seconds = time.perf_counter() - fetch_at
                            state, rows, detail = spot.validate_spot_snapshot(html, code, day)
                            if state == 'complete':
                                write_at = time.perf_counter()
                                spot.local_market_cache.save_spot_day(code, day, rows, state, spot.SOURCE_NAME, detail)
                                write_seconds = time.perf_counter() - write_at
                            failed = state in ('blocked','source_error')
                            spot._note_source(not failed)
                            tools.record_api_event('SpotBranch',status=500 if failed else 200,
                                                   latency=time.perf_counter()-fetch_at)
                        except spot.local_market_cache.DBError:
                            raise
                        except Exception as exc:
                            state, detail = 'source_error', tools.err_text(exc)
                            http_seconds = time.perf_counter() - fetch_at
                            spot._note_source(False)
                            tools.record_api_event('SpotBranch',status=500,latency=http_seconds)
                        finally:
                            spot._SEMAPHORE.release()
                            with guard:
                                concurrency['active'] -= 1
                        if state in ('blocked', 'source_error'):
                            concurrency['health_failed'] = True
                            spot.note_fetch_health(settings, failed=True, code=code)
                            if settings.get('parallel') == 12 and spot.fetch_settings()['parallel'] == 8 and time.monotonic() + spot.HTTP_READ_TIMEOUT < deadline:
                                if spot._acquire_source(deadline, background=False):
                                    try:
                                        repair_at = time.perf_counter()
                                        repair_html = fetch(code, day)
                                        state, rows, detail = spot.validate_spot_snapshot(repair_html, code, day)
                                        http_seconds += time.perf_counter() - repair_at
                                        if state == 'complete':
                                            write_at = time.perf_counter()
                                            spot.local_market_cache.save_spot_day(code, day, rows, state, spot.SOURCE_NAME, detail)
                                            write_seconds += time.perf_counter() - write_at
                                    except spot.local_market_cache.DBError:
                                        raise
                                    except Exception as exc:
                                        state, detail = 'source_error', tools.err_text(exc)
                                    finally:
                                        spot._SEMAPHORE.release()
                        with guard:
                            results.append({'date':day,'status':state,'detail':detail,
                                            'http_seconds':http_seconds,'wait_seconds':waiting,
                                            'write_seconds':write_seconds})
                        time.sleep(spot.REQUEST_GAP)
            except spot.local_market_cache.DBError:
                raise
            except Exception as exc:
                concurrency['setup_failed'] = True
                spot.note_fetch_health(settings, failed=True, code=code)
                print(f'⚠️ 測速工作連線失敗｜{code}｜{tools.err_text(exc)}',flush=True)
            finally:
                spot._FETCH_DEADLINE.at = 0
        with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='spot-speed-test') as pool:
            futures=[pool.submit(contextvars.copy_context().run,worker) for _ in range(workers)]
            for future in futures:
                future.result()
        elapsed=time.perf_counter()-started
        statuses=Counter(r['status'] for r in results)
        complete=statuses['complete']
        data={'code':code,'workers':workers,'peak_connections':concurrency['peak'],
              'requested_days':70,'attempted':len(results),'http_requests':concurrency['http_requests'],
              'complete_days':complete,'elapsed_seconds':round(elapsed,2),
              'days_per_second':round(complete/elapsed,2) if elapsed else 0,
              'mean_http_seconds':round(sum(r['http_seconds'] for r in results)/len(results),3) if results else None,
              'write_seconds_sum':round(sum(r['write_seconds'] for r in results),3),
              'wait_seconds_sum':round(sum(r['wait_seconds'] for r in results),3),
              'statuses':dict(statuses),'start_date':dates[0],'end_date':dates[-1],
              'cache_skipped':True,'all_complete':complete==70,'details':results}
        spot.note_fetch_health(settings, failed=concurrency['health_failed'] or concurrency['setup_failed'],
                               complete=complete == 70 and not concurrency['retry_seen'], code=code)
        data['adaptive_settings'] = spot.fetch_settings()
        print('⏱️ 現股70日測速｜'+json.dumps(data,ensure_ascii=False),flush=True)
        spot.local_market_cache.set_state('spot_fetch_benchmark:'+code,data)
        return data
    finally:
        if acquired:
            stock_lock.release()
        _BENCHMARK_LOCK.release()


def execute(request):
    if request['action']=='set':
        settings=spot.set_fetch_parallel(request['parallel'])
        return f"{'已重設自動降速機制。' if settings.get('auto') else '已切換手動模式。'}現股抓取已改為 {settings['parallel']} 條連線，全域上限同步調整。設定已保存，重啟後仍有效。"
    if request['action']=='status':
        settings=spot.fetch_settings()
        return (f"現股抓取：{settings['parallel']} 條連線，全域最多 {settings['global_limit']} 條。\n"
                f"自動模式：{'開啟' if settings.get('auto') else '關閉'}｜狀態：{ {'normal':'正常12條', 'fallback':'8條恢復觀察', 'probe':'已試回12條', 'locked':'固定8條，需手動解除'}.get(settings.get('phase'), '手動') if settings.get('auto') else '手動設定'}\n"
                f"恢復門檻：{settings.get('successes', 0)}/5 次完整70日成功\n"
                '解除固定：現股恢復自動連線\n調整：現股抓取改成8條\n測速：測試2330現股分點70日速度')
    code=request['code']
    if not code:
        return '請提供股票代號，例如：測試2330現股分點70日速度。'
    names=tools.get_stock_name_map()
    if code not in names:
        return f'無法確認股票代號 {code}，請檢查代號。'
    # A per-test setting is persisted only when explicitly present in the command.
    if request.get('parallel') is not None:
        spot.set_fetch_parallel(request['parallel'])
    data=benchmark(code)
    status='完整抓齊' if data['all_complete'] else '未完整抓齊'
    return (f"{code} {names[code]}｜現股分點70日測速\n"
            f"設定 {data['workers']} 條，實際最高 {data['peak_connections']} 條｜{data['start_date']}～{data['end_date']}\n"
            f"總耗時 {data['elapsed_seconds']:.2f} 秒｜{status} {data['complete_days']}/70 日\n"
            f"實際請求 {data['http_requests']} 次（{data['attempted']} 個日期）｜平均抓取 {data['mean_http_seconds']} 秒／頁\n"
            f"資料庫寫入累計 {data['write_seconds_sum']:.3f} 秒｜等待名額累計 {data['wait_seconds_sum']:.3f} 秒\n"
            '本次略過快取；完整日已保存。逐日耗時與錯誤詳情寫入 log。')
