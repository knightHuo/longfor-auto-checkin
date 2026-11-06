// User-operated official SMS login. Send only the member lzbToken to the parent process.
const API_ORIGIN='https://gw2c-hw-open.longfor.com';
const LOGIN_PATH='/mzapi-open-prod/web/api/weixin/lzbLoginByCode2';
const SMS_PATH='/mzapi-open-prod/web/api/weixin/getMobileCode';

function officialRequestPath(rawUrl){
  try{
    const url=new URL(rawUrl);
    if(url.origin!==API_ORIGIN||![LOGIN_PATH,SMS_PATH].includes(url.pathname))return null;
    return url.pathname;
  }catch(_){return null;}
}

function isLoginResponse(rawUrl){
  return officialRequestPath(rawUrl)===LOGIN_PATH;
}

function extractMemberSession(value){
  if(String(value?.errcode)!=='0')return null;
  // data.token is the separate U享家 session and must never be used as a fallback.
  const token=value?.data?.lzbToken;
  if(typeof token!=='string'||!/^[0-9a-f]{32}$/i.test(token))return null;
  return {token,source:'official-sms-lzb'};
}

function safeError(raw){
  if(!raw)return null;
  const value=String(raw);
  if(/^(?:net::)?ERR_[A-Z0-9_]{1,80}$/.test(value))return value;
  if(/^(?:invalid_json|invalid_login_response|business_-?\d{1,8})$/.test(value))return value;
  return 'request_failed';
}

function diagnostic(request,status=null,error=null){
  const path=officialRequestPath(request.url());
  if(!path)return null;
  let timing=null;
  try{
    const value=request.timing();
    if(Number.isFinite(value.responseEnd)&&value.responseEnd>=0)timing=value.responseEnd;
    else if(Number.isFinite(value.responseStart)&&value.responseStart>=0)timing=value.responseStart;
    else if(Number.isFinite(value.startTime)&&value.startTime>0)timing=Date.now()-value.startTime;
  }catch(_){/* Timing can be unavailable while a request is being disposed. */}
  const value={
    path,
    status:Number.isInteger(status)&&status>=100&&status<=599?status:null,
    error:safeError(error),
    timing_ms:Number.isFinite(timing)&&timing>=0&&timing<86400000?Math.round(timing):null,
  };
  if(path===LOGIN_PATH)value.source='member_token_field=lzbToken';
  return value;
}

function writeDiagnostic(request,status=null,error=null){
  const value=diagnostic(request,status,error);
  if(value)process.stderr.write(JSON.stringify(value)+'\n');
}

async function main(){
  const {chromium}=require('playwright-core');
  const browser=await chromium.launch({channel:'chrome',headless:false});
  const context=await browser.newContext({viewport:{width:480,height:860}});
  const page=await context.newPage();
  let saved=false;
  page.on('requestfailed',request=>{
    writeDiagnostic(request,null,request.failure()?.errorText||'request_failed');
  });
  page.on('response',async response=>{
    if(!officialRequestPath(response.url()))return;
    const request=response.request();
    const status=response.status();
    writeDiagnostic(request,status);
    if(saved||status<200||status>=300)return;
    try{
      const value=await response.json();
      if(String(value?.errcode)!=='0'){
        const code=String(value?.errcode??'');
        writeDiagnostic(request,status,/^-?\d{1,8}$/.test(code)?'business_'+code:'invalid_login_response');
        return;
      }
      if(!isLoginResponse(response.url()))return;
      const session=extractMemberSession(value);
      if(session){
        saved=true;
        const userAgent=await page.evaluate(()=>navigator.userAgent);
        process.stdout.write(JSON.stringify({...session,user_agent:userAgent})+'\n');
        await page.waitForTimeout(2000);
        await browser.close();
      }else writeDiagnostic(request,status,'invalid_login_response');
    }catch(_){writeDiagnostic(request,status,'invalid_json');}
  });
  await page.goto('https://u.longfor.com/mobile/mobileLogin',{waitUntil:'domcontentloaded',timeout:45000});
  process.stderr.write('Official login page opened; waiting for user-operated login.\n');
  await new Promise(resolve=>browser.on('disconnected',resolve));
  if(!saved)process.exitCode=2;
}

module.exports={officialRequestPath,isLoginResponse,extractMemberSession,diagnostic};
if(require.main===module){
  main().catch(()=>{process.stderr.write('Official login browser could not complete.\n');process.exitCode=1;});
}
