"""Offline 12-stock baseline/comparison charts from saved fixtures; does not fetch prices or use the real DB.
python eval/triangle_selection_check.py --output triangle_selection_output
Optional --before /path/to/baseline/kline_patterns.py produces comparison charts.
"""
import argparse,importlib.util,json,os,sys,tempfile,uuid,socket,platform
from pathlib import Path
platform.uname()
HERE=Path(__file__).resolve().parents[1]
def main():
 sys.stdout.reconfigure(encoding='utf-8')
 parser=argparse.ArgumentParser();parser.add_argument('--output',default='triangle_selection_output');parser.add_argument('--before',default=str(HERE/'eval'/'baselines'/'kline_patterns_round1_reference.py'))
 args=parser.parse_args();out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
 def tempdir(suffix=None,prefix=None,dir=None):
  p=Path(dir or out)/(str(prefix or 'tmp')+uuid.uuid4().hex+str(suffix or ''));p.mkdir(mode=0o777);return str(p)
 tempfile.mkdtemp=tempdir
 with tempfile.TemporaryDirectory(prefix='isolated_') as tmp:
  os.environ.update(DISCORD_AI_MARKET_CACHE_DB=str(Path(tmp)/'market.sqlite3'),DISCORD_AI_TRI_STATE_DB='0',MPLBACKEND='Agg')
  def blocked(*a,**k):raise RuntimeError('Offline verification forbids network/API')
  socket.create_connection=blocked;socket.socket.connect=blocked;socket.socket.connect_ex=blocked;socket.getaddrinfo=blocked
  sys.path.insert(0,str(HERE));sys.dont_write_bytecode=True
  import pandas as pd
  import kline_patterns as after,kline_debug as kd
  from PIL import Image,ImageDraw
  from answer_image import font
  engines=[('after',after)]
  if args.before:
   spec=importlib.util.spec_from_file_location('kline_patterns_before',args.before);before=importlib.util.module_from_spec(spec);sys.modules[spec.name]=before;spec.loader.exec_module(before);engines.insert(0,('before',before))
  fix=HERE/'eval'/'kline_fixtures';rows=[];photos={}
  names={'3034':'聯詠','2412':'中華電','2421':'建準','4977':'眾達-KY','2344':'華邦電','2467':'志聖','2474':'可成','3008':'大立光','3715':'定穎投控','4576':'大銀微系統','6173':'信昌電','1608':'華榮'}
  for label,engine in engines:
   kd.kline_patterns=engine
   for code in names:
    raw=pd.DataFrame(json.loads((fix/f'px_{code}.json').read_text(encoding='utf-8')));raw=raw[raw.Trading_Volume>0]
    frame=pd.DataFrame({k:raw[v].to_numpy(float) for k,v in {'Open':'open','High':'max','Low':'min','Close':'close','Volume':'Trading_Volume'}.items()},index=pd.to_datetime(raw.date))
    path=fix/f'actions_{code}.json';events=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status':'unverified'}
    if code!='3034':frame=frame.iloc[-141:]
    state=None
    days=frame.loc['2026-10-02':].index if code in ('3034','2474','1608') else [frame.index[-1]]
    # Saved observations from September; only the five requested October sessions are exported.
    if code in ('3034','2474','1608'):
     adjusted,_=engine.adjust(frame,events)
     for day in frame.loc['2026-09-01':'2026-10-01'].index:
      prefix=adjusted.loc[:day];new={};atr=engine._atr_prev(*(prefix[c].to_numpy(float) for c in ('High','Low','Close')))
      engine.user_triangle(prefix,atr,len(prefix)-1,state,new);state=json.loads(json.dumps(new))
    for day in days:
     prefix=frame.loc[:day];adjusted,_=engine.adjust(prefix,events);new={};atr=engine._atr_prev(*(adjusted[c].to_numpy(float) for c in ('High','Low','Close')))
     tri=engine.user_triangle(adjusted,atr,len(prefix)-1,state,new);state=json.loads(json.dumps(new))
     class Store:
      def get_state(self,key):return state
      def set_state_if_newer(self,*a,**k):raise AssertionError('Chart verification must remain read-only')
     engine.TRI_STATE_STORE=Store()
     panel=kd.build_panel(prefix,events,code,names[code],state_key=f'tri_state_{code}',state_write=False)['kline_debug']
     import member_pattern as mp, warrant_ai_tools as tools, answer_image as image
     detected=engine.detect(prefix,events,include_debug=True,state_write=False,state_key=f'tri_state_{code}')
     basis=mp.price_basis(detected,prefix,events)
     pic=kd.render(panel);date=str(day)[:10];filename=f'{code}_{date}_{label}.png';pic.save(out/filename);photos[(code,date,label)]=pic
     at=lambda edge:round(tri[edge][0]*(len(prefix)-1)+tri[edge][1],5) if tri else None
     rows.append({'code':code,'date':date,'version':label,'upper':at('upper'),'lower':at('lower'),'candidate':tri.get('candidate') if tri else None,'position':tri.get('current_position') if tri else None,'text':tri.get('text') if tri else None,'selection':tri.get('selection') if tri else None,'first_break':tri.get('first_break') if tri else None,'rules':engine.TRI_STATE_VERSION,'records':[{'recognized':r['recognized'],'F_date':r['F_date']} for r in state['records']],'image':filename,'basis_adjusted':bool(basis and basis['adjusted']),'formation':bool(state.get('formation'))})
     # Render the member overlay and key-price path locally, without Gemini/API.
     calc=tools.core()._calculate_weighted_volume_profile_stats
     display=prefix.copy()
     for n in (5,10,20,60): display[f'MA{n}']=display.Close.rolling(n).mean()
     for n in (5,20): display[f'MV{n}']=display.Volume.rolling(n).mean()
     shown=display.tail(70);stats=calc(shown,n_bins=40)
     if not stats: raise ValueError(f'{code}: no offline profile')
     chart={'stock_code':code,'stock_name':names[code],'hide_bollinger':True,
            'bars':[{'date':str(d)[:10],**{k:float(v) if pd.notna(v) else None for k,v in row.items()}} for d,row in shown.iterrows()],
            'volume_profile':{'bins':list(map(float,stats['bins'])),'profile':list(map(float,stats['profile'])),'max_idx':int(stats['max_idx']),'second_idx':int(stats['second_idx'])}}
     if basis and basis['adjusted']: mp.apply_price_basis(chart,basis,calc)
     view=mp.overlay(chart,mp.geometry(detected,prefix,events))
     if view: chart['member_triangle']=view
     chart['key_levels']=mp.levels_card(chart,{},tools.key_price_levels,tools.NEAR_ZONE_RATIO)
     image.render_answer(f'{code} 型態｜{label}離線圖面', '', [chart]).save(out/f'{code}_{date}_{label}_member.png')
     repeat={};repeated=engine.user_triangle(adjusted,atr,len(prefix)-1,state,repeat)
     if tri and repeated:
      if tri['upper']!=repeated['upper'] or tri['lower']!=repeated['lower'] or tri['current_position']!=repeated['current_position']:
       raise AssertionError(f'{code} {date}: same-day repeat changed geometry/state')
     elif bool(tri)!=bool(repeated): raise AssertionError(f'{code} {date}: same-day repeat lost/created triangle')
   engine.TRI_STATE_STORE=None
  if args.before:
   for code,date in sorted({(c,d) for c,d,_ in photos}):
    pair=[photos[(code,date,x)] for x in ('before','after')];w=900;thumbs=[]
    for im in pair:thumbs.append(im.resize((w,round(im.height*w/im.width))))
    canvas=Image.new('RGB',(w*2,max(i.height for i in thumbs)+60),'#fff');draw=ImageDraw.Draw(canvas)
    draw.text((20,10),'修改前',font=font(28,True),fill='#1C293D');draw.text((w+20,10),'長短候選比較版',font=font(28,True),fill='#1C293D')
    for i,im in enumerate(thumbs):canvas.paste(im,(i*w,60))
    canvas.save(out/f'{code}_{date}_comparison.png')
  (out/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
  print(json.dumps(rows,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
