"""Offline charts from saved fixtures; does not fetch prices or use the real DB.
python eval/triangle_round1_check.py --output triangle_round1_output
Optional --before /path/to/baseline/kline_patterns.py produces comparison charts.
"""
import argparse,importlib.util,json,os,sys,tempfile,uuid,socket,platform
from pathlib import Path
platform.uname()
HERE=Path(__file__).resolve().parents[1]
def main():
 sys.stdout.reconfigure(encoding='utf-8')
 parser=argparse.ArgumentParser();parser.add_argument('--output',default='triangle_round1_output');parser.add_argument('--before')
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
  names={'3034':'聯詠','2412':'中華電','2421':'建準','4977':'眾達-KY'}
  for label,engine in engines:
   kd.kline_patterns=engine
   for code in names:
    raw=pd.DataFrame(json.loads((fix/f'px_{code}.json').read_text(encoding='utf-8')));raw=raw[raw.Trading_Volume>0]
    frame=pd.DataFrame({k:raw[v].to_numpy(float) for k,v in {'Open':'open','High':'max','Low':'min','Close':'close','Volume':'Trading_Volume'}.items()},index=pd.to_datetime(raw.date))
    path=fix/f'actions_{code}.json';events=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status':'unverified'}
    if code!='3034':frame=frame.iloc[-141:]
    state=None
    days=frame.loc['2026-10-02':].index if code=='3034' else [frame.index[-1]]
    # Saved observations from September; only the five requested October sessions are exported.
    if code=='3034':
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
     pic=kd.render(panel);date=str(day)[:10];filename=f'{code}_{date}_{label}.png';pic.save(out/filename);photos[(code,date,label)]=pic
     at=lambda edge:round(tri[edge][0]*(len(prefix)-1)+tri[edge][1],5) if tri else None
     rows.append({'code':code,'date':date,'version':label,'upper':at('upper'),'lower':at('lower'),'candidate':tri.get('candidate') if tri else None,'position':tri.get('current_position') if tri else None,'text':tri.get('text') if tri else None,'records':[{'recognized':r['recognized'],'F_date':r['F_date']} for r in state['records']],'image':filename})
   engine.TRI_STATE_STORE=None
  if args.before:
   for code,date in sorted({(c,d) for c,d,_ in photos}):
    pair=[photos[(code,date,x)] for x in ('before','after')];w=900;thumbs=[]
    for im in pair:thumbs.append(im.resize((w,round(im.height*w/im.width))))
    canvas=Image.new('RGB',(w*2,max(i.height for i in thumbs)+60),'#fff');draw=ImageDraw.Draw(canvas)
    draw.text((20,10),'修改前',font=font(28,True),fill='#1C293D');draw.text((w+20,10),'第一輪修改後',font=font(28,True),fill='#1C293D')
    for i,im in enumerate(thumbs):canvas.paste(im,(i*w,60))
    canvas.save(out/f'{code}_{date}_comparison.png')
  (out/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
  print(json.dumps(rows,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
