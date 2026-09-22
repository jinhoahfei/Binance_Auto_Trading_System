from pathlib import Path
from decimal import Decimal as D,getcontext
from datetime import datetime,timezone,timedelta
from collections import Counter
import json,sqlite3
getcontext().prec=34
out=Path(__file__).parent;raw=json.loads((out/'public-candles-full.json').read_text())['data'];db=sqlite3.connect(out/'audit.sqlite');db.row_factory=sqlite3.Row
ks=[{'open_ms':v[0],'open':v[1],'high':v[2],'low':v[3],'close':v[4],'volume':v[5]} for v in raw];idx={k['open_ms']:i for i,k in enumerate(ks)}
def ms(s):return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()*1000)
def band(cs):
 mean=sum(cs)/20;sd=(sum((c-mean)**2 for c in cs)/20).sqrt();return mean-2*sd,mean+2*sd
cache={}
def final(i):
 if i in cache:return cache[i]
 cs=[D(k['close']) for k in ks[max(0,i-499):i+1]];ema=sum(cs[:9])/9;es=[ema]
 for p in cs[9:]:ema=D('.2')*p+D('.8')*ema;es.append(ema)
 last=es[-6:];xm=D('2.5');ym=sum(last)/6;slope=sum((D(j)-xm)*(v-ym) for j,v in enumerate(last))/sum((D(j)-xm)**2 for j in range(6));slope=(slope/cs[-1]*100).quantize(D('.00000001'))
 lo,hi=band(cs[-20:]);pb=(cs[-1]-lo)/(hi-lo);low=D(ks[i]['low']);prev=min(D(k['low']) for k in ks[i-3:i]);ret={'slope':str(slope),'pct_b':str(pb),'low':str(low),'previous3_min':str(prev),'signal_conditions':slope>D('-.03') and pb>D('.25') and low>=prev,'candles_for_ema':len(cs)};cache[i]=ret;return ret
mismatches=[];n=0
for r in db.execute('select * from candles'):
 k=json.loads(r['data']);i=idx.get(ms(k['open_time']))
 if i is None:continue
 n+=1
 if any(D(k[f])!=D(ks[i][f]) for f in ('open','high','low','close')):mismatches.append({'file':r['file'],'line':r['line'],'raw':k,'public':ks[i]})
checks=[]
for r in db.execute("select * from evaluations where bstate='B_WAIT_SIGNAL' and closed=1 order by time"):
 x=json.loads(r['data']);m=x['market'];i=idx[ms(m['current_30m_candle_id'].split('30m:')[1])];calc=final(i);assert D(calc['slope'])==D(m['ema_slope_30m_close']),(r['time'],calc,m['ema_slope_30m_close']);assert abs(D(calc['pct_b'])-D(m['pct_b_close']))<D('1e-20');assert D(calc['low'])==D(m['current_closed_candle_low']);assert D(calc['previous3_min'])==min(map(D,m['previous_3_closed_candle_lows']))
 checks.append({'time':r['time'],'file':r['file'],'line':r['line'],'logged':{f:m[f] for f in ('ema_slope_30m_close','pct_b_close','current_closed_candle_low','previous_3_closed_candle_lows')},'recalculated':calc})
touches=[]
for r in db.execute("select * from evaluations where ids like '%G-02%' or ids like '%G-03%'"):
 x=json.loads(r['data']);m=x['market'];i=idx[ms(m['current_30m_candle_id'].split('30m:')[1])];price=D(m['realtime_price']);cs=[D(k['close']) for k in ks[i-19:i]]+[price];lo,hi=band(cs);bbw=(hi-lo)/(sum(cs)/20);assert abs(bbw-D(m['touch_candle_bbw']))<D('1e-20');assert price<=lo
 touches.append({'time':r['time'],'file':r['file'],'line':r['line'],'price':str(price),'lower':str(lo),'bbw':str(bbw),'b_eligible':bbw<D('.02')})
res={'public_data_overlap_rows':n,'public_data_ohlc_mismatches':mismatches,'signal_close_checks':checks,'touches':touches,'formula':'Population BB20; EMA9 seeded SMA9 then alpha0.2, OLS6 normalized/close*100, quantize8. Uses at most500 past closes; insufficient500 at earliest targets still verified matches logged8dp.'}
(out/'public-recalculation.json').write_text(json.dumps(res,ensure_ascii=False,indent=2)+'\n')
print('public_overlap_rows',n,'mismatches',len(mismatches),'signal_close_all_four_inputs_verified',len(checks),'touches_verified',len(touches))
for r in checks:print(r['time'],r['recalculated'])
