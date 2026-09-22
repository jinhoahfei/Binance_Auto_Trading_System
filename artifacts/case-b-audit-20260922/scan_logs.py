"""Read-only audit of saved live logs; produces analysis artifacts only."""
from pathlib import Path
from collections import Counter,defaultdict
from decimal import Decimal as D
import json,sqlite3,time
ROOT=Path('/Users/oscar/Desktop/Binance_Auto'); OUT=ROOT/'artifacts/case-b-audit-20260922'
files=[(p,p.stat().st_size) for p in sorted((ROOT/'Log_History').glob('*_live_*.log'))]
db=sqlite3.connect(OUT/'audit.sqlite');db.execute('pragma journal_mode=OFF');db.execute('pragma synchronous=OFF')
for t in ('evaluations','candles','events','files'):db.execute(f'DROP TABLE IF EXISTS {t}')
db.execute('CREATE TABLE evaluations(run TEXT, session TEXT, time TEXT, seq INTEGER, file TEXT, line INTEGER, root TEXT, bstate TEXT, cstate TEXT, status TEXT, lower_id TEXT, closed INTEGER, signal_ok INTEGER, buy_ok INTEGER, ids TEXT, data TEXT)')
db.execute('CREATE TABLE candles(run TEXT,time TEXT,version INTEGER,file TEXT,line INTEGER,open_time TEXT,closed INTEGER,data TEXT)')
db.execute('CREATE TABLE events(run TEXT,time TEXT,event TEXT,file TEXT,line INTEGER,data TEXT)')
db.execute('CREATE TABLE files(file TEXT,size INTEGER,last_line INTEGER,last_seq INTEGER,last_time TEXT)')
runs=defaultdict(lambda:{'events':Counter(),'eval_states':Counter(),'transitions':Counter(),'first':None,'last':None,'evals':0,'files':0,'bad_json':0,'last_seq':None,'sequence_gaps':[],'touches':0,'signals':0,'buy_signals':0})
important=[]; anomalies=[]; batch=[]; crows=[]; erows=[];start=time.monotonic()
# The event field precedes details in these logger records.
def event_of(l):
 i=l.find('"event":"');return l[i+9:l.find('"',i+9)] if i>=0 else ''
ignorable={'market_boundary','event_processed','event_queued','event_dispatched','action_requested','action_completed','market_evaluation_deferred','market_evaluation_scheduled','market_input_observed','strategy_evaluated'}
for fi,(p,size) in enumerate(files):
 run=p.stem.split('_')[-2];r=runs[run];r['files']+=1;pos=0;last_seq=None;last_time=None; n=0
 with p.open('rb') as f:
  for n,lb in enumerate(f,1):
   pos+=len(lb)
   if pos>size:break
   l=lb.decode();event=event_of(l);r['events'][event]+=1
   # Cheap field extraction for scope and continuity, independent of details parsing.
   try:
    t=l.split('"timestamp_kst":"',1)[1].split('"',1)[0];seq=int(l.split('"sequence":',1)[1].split(',',1)[0])
   except (IndexError,ValueError):r['bad_json']+=1;continue
   if r['first'] is None:r['first']=t
   r['last']=t;last_time=t;last_seq=seq
   if r['last_seq'] is not None and seq!=r['last_seq']+1:r['sequence_gaps'].append([r['last_seq'],seq,p.name,n])
   r['last_seq']=seq
   if event=='market_input_observed':
    if '"interval":"30m"' not in l or '"closed":true' not in l:continue
    obj=json.loads(l);x=obj['details']
    for k in x.get('klines',[]):
     if k['interval']=='30m' and k['closed']:crows.append((run,t,x.get('market_version'),p.name,n,k['open_time'],1,json.dumps(k)))
    continue
   if event=='strategy_evaluated':
    obj=json.loads(l);x=obj['details'];v=x.get('evaluation',{});m=v.get('market',{});rt=v.get('runtime',{});state=x.get('state_before',{});after=x.get('state_after',{});ids=x.get('transition_ids',[])
    r['evals']+=1;r['eval_states'][str(state.get('root_state'))+':'+str(state.get('case_b_signal_state'))]+=1;r['transitions'].update(ids)
    if 'G-02' in ids or 'G-03' in ids:r['touches']+=1
    if 'B-05' in ids:r['signals']+=1
    if 'B-06' in ids or 'B-09' in ids:r['buy_signals']+=1
    lows=m.get('previous_3_closed_candle_lows') or []
    sig=bool(m.get('confirmed_30m_close')) and len(lows)==3 and D(m['ema_slope_30m_close'])>D('-.03') and D(m['pct_b_close'])>D('.25') and D(m['current_closed_candle_low'])>=min(map(D,lows))
    buy=(state.get('case_b_signal_state')=='B_WAIT_PULLBACK' and D(m.get('signal_elapsed','0'))<=10800 and D(m.get('realtime_pct_b','0'))<=D('.30') and rt.get('position_owner') is None and rt.get('pending_order_id') is None and not rt.get('case_b_entry_paused'))
    compact={'event_id':x.get('event_id'),'market_version':x.get('market_version'),'selected_regime':x.get('selected_regime'),'state_before':state,'state_after':after,'market':{k:val for k,val in m.items() if k!='condition_timers'},'runtime':rt,'position':v.get('position'),'pending_order':v.get('pending_order'),'condition_comparisons':v.get('condition_comparisons'),'runtime_after':x.get('runtime_after'),'action_requests':x.get('action_requests')}
    batch.append((run,x.get('session_id'),t,seq,p.name,n,state.get('root_state'),state.get('case_b_signal_state'),state.get('case_c_signal_state'),x.get('session_status'),x.get('lower_event_id'),int(bool(m.get('confirmed_30m_close'))),int(sig),int(buy),json.dumps(ids),json.dumps(compact)))
    if (sig and state.get('case_b_signal_state')=='B_WAIT_SIGNAL' and 'B-05' not in ids) or (buy and not set(ids)&{'B-06','B-09'}):anomalies.append({'file':p.name,'line':n,'time':t,'ids':ids,'signal_ok':sig,'buy_ok':buy,'event_id':x.get('event_id')})
    continue
   if event not in ignorable and not event.startswith('chart_'):
    try:obj=json.loads(l)
    except json.JSONDecodeError:r['bad_json']+=1;continue
    x=obj.get('details',{})
    erows.append((run,t,event,p.name,n,json.dumps(x)))
    if event=='runtime_configured':r['configuration']=x
 if batch:db.executemany('INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',batch);batch=[]
 if crows:db.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?)',crows);crows=[]
 if erows:db.executemany('INSERT INTO events VALUES(?,?,?,?,?,?)',erows);erows=[]
 db.execute('INSERT INTO files VALUES(?,?,?,?,?)',(p.name,size,n,last_seq,last_time));db.commit()
 if fi%100==0:print('scanned_files',fi+1,'of',len(files),'seconds',round(time.monotonic()-start),flush=True)
for col in ('run','time','bstate','closed','signal_ok','buy_ok'):db.execute(f'CREATE INDEX idx_eval_{col} ON evaluations({col})')
db.execute('CREATE INDEX idx_candle ON candles(run,open_time)');db.execute('CREATE INDEX idx_events ON events(run,event)');db.commit()
summary={'files':len(files),'bytes':sum(s for p,s in files),'seconds':time.monotonic()-start,'runs':dict(runs),'apparent_condition_mismatches':anomalies}
(OUT/'scan-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print('DONE','runs',len(runs),'evals',sum(r['evals'] for r in runs.values()),'mismatches',len(anomalies),'seconds',round(time.monotonic()-start),flush=True)
for run,r in runs.items():
 if r['evals']:print(run,r['first'],r['last'],'evals',r['evals'],'touches',r['touches'],'signals',r['signals'],'buys',r['buy_signals'],dict(r['eval_states']),flush=True)
print('anomalies',anomalies[:20],flush=True)
