"""Local-first responsive control panel. No order POST before explicit web arming."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .exchange.bitget import Bitget, ExchangeError
from .exchange.store import StoreBridge
from .runner import make_runner

DATA = Path(os.environ.get('ETH1003_DATA_DIR', str(Path.home() / '.eth1003'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
AUTH = DATA / 'auth.json'
STATIC = Path(__file__).parent / 'static'
PROJECT = Path(__file__).parent.parent
app = FastAPI(title='ETH1003', docs_url=None, redoc_url=None)
runtime = {'mode':'demo','store':None,'bitget':None,'runner':None,'armed':False,
           'automatic':False,'task':None,'sessions':{},'last_result':None,
           'events':deque(maxlen=100),'lock':asyncio.Lock()}
ZH_ERRORS={
    'TARGET_LEVERAGE_NOT_SUPPORTED':'交易所合約或這筆金額的倉位階梯不支援 150 倍槓桿；本次沒有下單。',
    'EXISTING_EXCHANGE_POSITION':'Bitget 已有 ETH 持倉；為避免重複開倉，本次沒有下單。',
    'EXISTING_EXCHANGE_ORDER':'Bitget 已有 ETH 委託；請先確認原單狀態。',
    'UNOWNED_EXCHANGE_PLAN':'Bitget 有未歸屬的止盈止損單；請先核對。',
    'INSUFFICIENT_AVAILABLE_MARGIN':'帳戶可用保證金不足；本次沒有下單。',
    'BELOW_EXCHANGE_MINIMUM':'依回測的 0.01 ETH 取整後，金額未達交易所最低下單量。',
    'BELOW_BACKTEST_0P01_ETH_STEP':'帳戶規模不足以按回測規則開出 0.01 ETH。',
    'MARKET_REFERENCE_PRICE_DRIFT':'報價在準備期間變動超過 0.5%；本次沒有下單。',
    'SIGNAL_HOUR_EXPIRED':'目前已不是剛完成的那根小時線；不回填過去價格下單。',
    'RECONCILIATION_REQUIRED':'前一筆委託尚未確認交易所狀態；暫停新單，避免重複下單。',
    'UNRESOLVED_EXCHANGE_STATE':'訂單或持倉狀態尚未確認；暫停新單。',
    'POSITION_EXCEEDS_TRACKED_FILL':'交易所持倉多於本程式確認的成交量；暫停自動操作。',
    'ORDER_DETAIL_MISSING_FILLED_QUANTITY':'交易所訂單明細沒有成交數量；無法安全確認。',
    'NEED_3600_COMPLETE_HOURLY_BARS':'歷史小時線不足 3600 根，暫停判斷。',
    'BINANCE_HOURLY_HISTORY_GAP':'Binance 歷史小時線有缺口；暫停判斷。',
    'BINANCE_HOURLY_HISTORY_INCOMPLETE':'Binance 歷史小時線不完整；暫停判斷。',
    'LATEST_COMPLETE_BINANCE_HOUR_UNAVAILABLE':'最新已完成小時線還取不到；稍後再試。',
    'NO_OWNED_ENTRY':'找不到本程式擁有的進場單；不會平掉未知持倉。',
    'ENTRY_NOT_CONFIRMED_OPEN':'進場單尚未確認成交與持倉，暫不平倉。',
    'POSITION_NOT_UNIQUE':'交易所有多筆或沒有可唯一辨識的 ETH 持倉；暫停操作。',
    'CLOSE_OID_ALREADY_USED':'這筆平倉指令已送出或結果待確認，不會重送。',
    'CLIENT_OID_ALREADY_USED':'這根小時線的開倉指令已存在，不會重複下單。',
    'ADOPTION_REQUIRES_ONEWAY_CROSS_150':'既有持倉不是單向、全倉、150 倍的組合，不能安全接管。',
    'POSITION_LEVERAGE_NOT_150':'既有持倉的實際槓桿不是 150 倍，不能接管。',
    'LOCAL_ENTRY_ALREADY_ACTIVE':'本程式已有未結束的進場單或持倉，不能重複接管。',
    'UNOWNED_EXCHANGE_PLAN':'交易所還有未知的止盈止損委託，請先處理再接管。',
    'WAITING_FOR_ENTRY_CANCEL':'正在等交易所確認未成交餘量已取消，暫不重送。',
    'PREVIOUS_CLOSE_NOT_CONFIRMED':'上一筆平倉尚未確認成交，暫不再送一筆。',
}

def human_error(exc):
    message=str(exc)
    for code,zh in ZH_ERRORS.items():
        if code in message:return zh
    if isinstance(exc,ExchangeError):
        prefix='交易所回應未確定，請核對原單，勿重送。' if exc.uncertain else 'Bitget 拒絕或無法完成操作。'
        return prefix+' 交易所代碼：'+str(exc.code)+'。原始回應：'+str(exc.message)
    if any(ord(char)>127 for char in message):return message
    return '操作未完成，請查看訂單與連線狀態。技術代碼：'+message[:160]


def present_result(value:dict):
    if not isinstance(value,dict):return value
    action=value.get('action')
    descriptions={'hold':'訊號維持做多，交易所持倉不變。',
                  'flat':'訊號空手，目前沒有本程式持有的多單。',
                  'enter':'訊號要求開多；請查看下方委託與成交確認。',
                  'exit':'訊號要求平倉；已按持倉送 reduce-only，請確認成交。',
                  'cancel_unfilled_remainder':'訊號反轉，先取消尚未成交的進場餘量。',
                  'blocked':ZH_ERRORS.get(value.get('reason'),
                       '條件未確認，這次沒有安全送出新單。')}
    return {'中文說明':descriptions.get(action,'決策已完成，請查看狀態。'),**value}


class Password(BaseModel):
    password: str


class Connect(BaseModel):
    mode: str
    account_type: str = 'auto'
    key: str
    secret: str
    passphrase: str


class Arm(BaseModel):
    phrase: str


class Toggle(BaseModel):
    enabled: bool


class Adopt(BaseModel):
    phrase: str


class Manage(BaseModel):
    entry_oid: str
    plan_oid: str | None = None
    fraction: str = '1'
    kind: str | None = None
    price: str | None = None
    qty: str | None = None


def _local(request: Request):
    return request.client and request.client.host in {'127.0.0.1','::1'}


def _password_record(password: str):
    if len(password) < 12:raise HTTPException(400, '密碼至少 12 字元')
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac('sha256',password.encode(),salt,300_000)
    return {'salt':salt.hex(),'hash':digest.hex()}


def _check_password(password: str):
    if not AUTH.exists():return False
    record=json.loads(AUTH.read_text(encoding='utf-8'))
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(record['salt']),300_000)
    return hmac.compare_digest(digest.hex(),record['hash'])


def _auth(request: Request):
    token=request.cookies.get('eth1003_session','')
    session=runtime['sessions'].get(token)
    if not session or session['expires'] < time.time():
        raise HTTPException(401,'請先登入')
    if request.method not in {'GET','HEAD'} and request.headers.get('x-csrf-token') != session['csrf']:
        raise HTTPException(403,'CSRF 驗證失敗')
    return session


def _event(message: str, **data):
    runtime['events'].appendleft({'at':time.time(),'message':message,'data':data})


def _connected():
    if runtime['bitget'] is None:raise HTTPException(409,'請先於網站連接 Bitget 帳戶')
    return runtime['bitget'],runtime['runner']


def _armed():
    if not runtime['armed']:raise HTTPException(409,'交易未啟用；請在網站輸入啟用確認文字')


@app.get('/')
async def home():
    return FileResponse(STATIC / 'index.html')


@app.get('/daily.csv')
async def daily_csv(request:Request):
    _auth(request)
    return FileResponse(STATIC / 'eth_1p35_daily.csv',media_type='text/csv',
                        filename='ETH_1p35_歷史每日盈虧.csv')


@app.get('/api/bootstrap')
async def bootstrap(request:Request):
    return {'needs_setup':not AUTH.exists(),'local':bool(_local(request)),
            'authenticated':bool(request.cookies.get('eth1003_session') in runtime['sessions'])}


@app.get('/api/session')
async def session(request:Request):
    return {'csrf':_auth(request)['csrf']}


@app.post('/api/setup')
async def setup(request:Request,payload:Password):
    if AUTH.exists() or not _local(request):raise HTTPException(403,'首次設定只可在本機完成')
    record=_password_record(payload.password)
    fd=os.open(AUTH,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as file:json.dump(record,file)
    return {'ok':True}


@app.post('/api/login')
async def login(payload:Password):
    if not _check_password(payload.password):
        await asyncio.sleep(.5)
        raise HTTPException(401,'密碼錯誤')
    token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24)
    runtime['sessions'][token]={'expires':time.time()+12*3600,'csrf':csrf}
    response=JSONResponse({'ok':True,'csrf':csrf})
    response.set_cookie('eth1003_session',token,httponly=True,samesite='strict',max_age=12*3600)
    return response


@app.post('/api/logout')
async def logout(request:Request):
    _auth(request)
    runtime['sessions'].pop(request.cookies.get('eth1003_session'),None)
    response=JSONResponse({'ok':True});response.delete_cookie('eth1003_session')
    return response


@app.get('/api/status')
async def status(request:Request):
    _auth(request)
    runner=runtime['runner']
    return {'mode':runtime['mode'],'connected':runner is not None,
            'armed':runtime['armed'],'automatic':runtime['automatic'],
            'last_result':present_result(runtime['last_result']) if runtime['last_result'] else None,
            'orders':runner.execution.ledger.rows() if runner else [],
            'events':list(runtime['events']),
            'strategy':{'symbol':'ETHUSDT','signal':'1h EMA12 > EMA720; long or flat',
                        'allocation_pct':1.35,'leverage':150,'qty_floor_eth':'0.01',
                        'fixed_tp':None,'fixed_sl':None,
                        'source':'Binance perpetual close; Bitget execution quote'}}


@app.post('/api/connect')
async def connect(request:Request,payload:Connect):
    _auth(request)
    if payload.mode not in {'demo','live'} or payload.account_type not in {'auto','classic','uta'}:
        raise HTTPException(400,'模式不正確')
    if not all([payload.key,payload.secret,payload.passphrase]):
        raise HTTPException(400,'請填完整 API 資料')
    async with runtime['lock']:
        runtime['automatic']=False;runtime['armed']=False
        if runtime['bitget']:await runtime['bitget'].close()
        prefix='DEMO' if payload.mode=='demo' else 'BITGET'
        env={f'ETH1003_{prefix}_{key}':value for key,value in [
            ('KEY',payload.key),('SECRET',payload.secret),('PASSPHRASE',payload.passphrase)]}
        store=StoreBridge(payload.account_type,env)
        bitget=Bitget(store,payload.mode)
        runtime.update(mode=payload.mode,store=store,bitget=bitget,
                       runner=make_runner(bitget,DATA / payload.mode))
        _event('帳戶資料僅保存在程式記憶體；交易仍關閉',mode=payload.mode)
    return {'ok':True,'mode':payload.mode,'armed':False}


@app.get('/api/public-contract')
async def public_contract(request:Request):
    _auth(request)
    bitget=Bitget(StoreBridge(),'paper')
    try:
        instrument=await bitget.instrument('ETHUSDT','classic')
        tier=await bitget.tier('ETHUSDT',200,'classic')
        supported=min(int(instrument['max_leverage']),tier)>=150
        return {'中文說明':('目前公開商品與 200 U 名目階梯均顯示可用 150 倍；仍須以帳戶設定後回讀為準。'
                           if supported else '目前公開商品或 200 U 名目階梯不支援 150 倍，系統會擋單。'),
                'instrument':instrument,'tier_at_200_usdt':tier,
                'supports_150_at_200_usdt':supported}
    finally:await bitget.close()


@app.post('/api/self-test')
async def self_test(request:Request):
    _auth(request)
    proc=await asyncio.create_subprocess_exec(sys.executable,'-m','pytest','-q','tests',
        cwd=PROJECT,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    try:output,_=await asyncio.wait_for(proc.communicate(),timeout=90)
    except TimeoutError:
        proc.kill();await proc.wait()
        raise HTTPException(504,'自動測試超時，沒有進行任何下單')
    return {'通過':proc.returncode==0,'中文說明':('本機模擬測試通過；尚非 Bitget 實際接單驗證。'
            if proc.returncode==0 else '本機模擬測試失敗；請勿啟用下單。'),
            '測試輸出':output.decode(errors='replace')[-6000:]}


@app.post('/api/replay-backtest')
async def replay_backtest(request:Request):
    _auth(request)
    proc=await asyncio.create_subprocess_exec(sys.executable,str(PROJECT/'research'/'replay_frozen.py'),
        cwd=PROJECT,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    try:output,_=await asyncio.wait_for(proc.communicate(),timeout=60)
    except TimeoutError:
        proc.kill();await proc.wait()
        raise HTTPException(504,'凍結歷史重播超時；沒有進行任何下單')
    if proc.returncode:
        raise HTTPException(500,'凍結歷史重播未通過原結果核對，請勿將網站數字視為已驗證')
    return {'中文說明':'固定 1.35% 凍結歷史重播與原報告吻合；不是未來獲利預測。',
            '結果':json.loads(output)}


@app.get('/api/diagnostics')
async def diagnostics(request:Request):
    _auth(request);bitget,_=_connected()
    return await bitget.diagnostics('ETHUSDT')


@app.get('/api/exchange-snapshot')
async def exchange_snapshot(request:Request):
    _auth(request);bitget,_=_connected()
    snapshot=await bitget.account_snapshot('ETHUSDT')
    snapshot['balance'].pop('raw',None)
    return snapshot


@app.post('/api/reconcile')
async def reconcile(request:Request):
    _auth(request);_,runner=_connected()
    output=[]
    async with runtime['lock']:
        for row in sorted(runner.execution.ledger.rows(),key=lambda x:0 if x['kind']=='close' else 1):
            if row['kind'] in {'entry','close'} and row['state'] not in {'blocked','rejected','canceled','cancelled','closed'}:
                try:output.append({'clientOid':row['oid'],'result':await runner.execution.reconcile(row['oid'])})
                except Exception as exc:output.append({'clientOid':row['oid'],'error':human_error(exc)})
    return {'results':output}


@app.post('/api/adopt')
async def adopt(request:Request,payload:Adopt):
    _auth(request);_connected()
    if payload.phrase!='接管既有 ETH 多單':
        raise HTTPException(400,'請輸入：接管既有 ETH 多單')
    async with runtime['lock']:
        runtime['automatic']=False
        value=await runtime['runner'].execution.adopt_position(allow_adopt=True)
        _event('已經核對並接管既有 ETH 多單；自動交易仍關閉',clientOid=value['order']['id'])
        return value


@app.get('/api/signal')
async def signal(request:Request):
    _auth(request);_,runner=_connected()
    data,now_ms=await runner.signal()
    return {'signal':data.__dict__,'observed_ms':now_ms,
            'lag_ms':now_ms-data.decided_at_ms}


@app.post('/api/preview')
async def preview(request:Request):
    _auth(request);_,runner=_connected()
    async with runtime['lock']:
        value=await runner.decide(allow_post=False)
        runtime['last_result']=value
        return present_result(value)


@app.post('/api/arm')
async def arm(request:Request,payload:Arm):
    _auth(request);_connected()
    expected='ENABLE DEMO TRADING' if runtime['mode']=='demo' else 'ENABLE LIVE TRADING'
    if payload.phrase != expected:raise HTTPException(400,f'請輸入 {expected}')
    runtime['armed']=True
    _event('已在網站啟用下單',mode=runtime['mode'])
    return {'armed':True,'mode':runtime['mode']}


@app.post('/api/disarm')
async def disarm(request:Request):
    _auth(request);runtime['armed']=False;runtime['automatic']=False
    _event('已停止自動新單；既有持倉仍須管理')
    return {'armed':False,'automatic':False}


@app.post('/api/run')
async def run(request:Request):
    _auth(request);_armed();_,runner=_connected()
    async with runtime['lock']:
        value=await runner.decide(allow_post=True)
        runtime['last_result']=value
        _event('手動執行一次策略決策',action=value.get('action'))
        return present_result(value)


async def _auto_loop():
    while True:
        await asyncio.sleep(30)
        if not runtime['automatic'] or not runtime['armed'] or runtime['runner'] is None:
            continue
        try:
            async with runtime['lock']:
                value=await runtime['runner'].decide(allow_post=True)
                runtime['last_result']=value
                if value.get('action') in {'enter','exit','blocked'}:
                    _event('自動策略決策',action=value.get('action'),
                           reason=ZH_ERRORS.get(value.get('reason'),'已完成'))
        except Exception as exc:
            runtime['automatic']=False
            _event('自動執行失敗，已停用',error=human_error(exc))


@app.post('/api/automatic')
async def automatic(request:Request,payload:Toggle):
    _auth(request)
    if payload.enabled:_armed();_connected()
    runtime['automatic']=payload.enabled
    if payload.enabled and (runtime['task'] is None or runtime['task'].done()):
        runtime['task']=asyncio.create_task(_auto_loop())
    _event('自動執行設定已更新',enabled=payload.enabled)
    return {'automatic':payload.enabled}


@app.post('/api/close')
async def close(request:Request,payload:Manage):
    _auth(request);_armed();_,runner=_connected()
    async with runtime['lock']:
        result=await runner.execution.close(payload.entry_oid,int(time.time()*1000),
                                            fraction=payload.fraction,allow_post=True)
        runtime['automatic']=False
        _event('已送出只減倉平倉；等待交易所確認成交',clientOid=result['order']['id'])
        return result


@app.post('/api/cancel')
async def cancel(request:Request,payload:Manage):
    _auth(request);_armed();_,runner=_connected()
    async with runtime['lock']:
        return await runner.execution.cancel(payload.entry_oid,int(time.time()*1000),allow_post=True)


@app.post('/api/modify')
async def modify(request:Request,payload:Manage):
    _auth(request);_armed();_,runner=_connected()
    if not payload.price or not payload.qty:raise HTTPException(400,'需填新價格及數量')
    async with runtime['lock']:
        return await runner.execution.modify_limit(payload.entry_oid,int(time.time()*1000),
                                                   payload.price,payload.qty,allow_post=True)


@app.post('/api/protection')
async def protection(request:Request,payload:Manage):
    _auth(request);_armed();_,runner=_connected()
    if not payload.price or payload.kind not in {'sl','tp'}:raise HTTPException(400,'需填 SL/TP 與價格')
    async with runtime['lock']:
        return await runner.execution.add_protection(payload.entry_oid,int(time.time()*1000),
                                                     payload.kind,payload.price,allow_post=True)


@app.post('/api/cancel-plan')
async def cancel_plan(request:Request,payload:Manage):
    _auth(request);_armed();_,runner=_connected()
    if not payload.plan_oid:raise HTTPException(400,'請填入本程式建立的止盈止損委託編號')
    async with runtime['lock']:
        return await runner.execution.cancel_protection(payload.plan_oid,
                    int(time.time()*1000),allow_post=True)


@app.exception_handler(Exception)
async def errors(request:Request,exc:Exception):
    if isinstance(exc,HTTPException):
        return JSONResponse({'error':exc.detail},status_code=exc.status_code)
    message=human_error(exc)
    if runtime['store']:message=runtime['store'].redact(message)
    _event('操作失敗',error=message)
    return JSONResponse({'error':message},status_code=500)


@app.exception_handler(RequestValidationError)
async def validation_error(request:Request,exc:RequestValidationError):
    labels={'password':'密碼','mode':'交易模式','account_type':'帳戶類型',
            'key':'交易所金鑰','secret':'交易所密鑰','passphrase':'通行短語',
            'entry_oid':'進場委託編號','plan_oid':'止盈止損委託編號',
            'fraction':'平倉比例','price':'價格','qty':'數量','phrase':'確認文字'}
    missing=[]
    for item in exc.errors():
        field=str(item.get('loc',('欄位',))[-1]);missing.append(labels.get(field,field))
    return JSONResponse({'error':'表單資料不完整或格式錯誤：'+'、'.join(dict.fromkeys(missing))},
                        status_code=422)
