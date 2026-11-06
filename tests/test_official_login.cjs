// Offline checks only: requiring the helpers must never launch Chrome.
const test=require('node:test');
const assert=require('node:assert/strict');
const {officialRequestPath,isLoginResponse,extractMemberSession,diagnostic}=require('../tools/official_login.cjs');
const origin='https://gw2c-hw-open.longfor.com';
const login='/mzapi-open-prod/web/api/weixin/lzbLoginByCode2';
const sms='/mzapi-open-prod/web/api/weixin/getMobileCode';

test('accepts the verified official login URL and not the SMS response',()=>{
  assert.equal(isLoginResponse(origin+login),true);
  assert.equal(isLoginResponse(origin+sms),false);
  assert.equal(officialRequestPath(origin+sms),sms);
});

test('rejects old host, lookalikes and path prefixes',()=>{
  for(const url of [
    'https://u.longfor.com/web/api/weixin/lzbLoginByCode2',
    'https://gw2c-hw-open.longfor.com.example.invalid'+login,
    'http://gw2c-hw-open.longfor.com'+login,
    origin+login+'-other',origin+login+'/',
    'not a URL',
  ])assert.equal(isLoginResponse(url),false,url);
});

test('diagnostics discard URL query and whitelist network errors',()=>{
  const request={
    url:()=>origin+sms+'?phone=fake-private-phone&token=fake-private-token',
    timing:()=>({responseStart:3123.4,responseEnd:-1}),
  };
  const value=diagnostic(request,null,'net::ERR_CONNECTION_TIMED_OUT');
  assert.deepEqual(value,{path:sms,status:null,error:'net::ERR_CONNECTION_TIMED_OUT',timing_ms:3123});
  assert.doesNotMatch(JSON.stringify(value),/fake-private|\?|phone|token/);
  assert.equal(diagnostic(request,200,'URL contains fake-private-token').error,'request_failed');
});

test('diagnostics retain only known fields and numeric business error',()=>{
  const request={url:()=>origin+login,timing:()=>({responseEnd:125.7})};
  assert.deepEqual(diagnostic(request,200,'business_909001'),{
    path:login,status:200,error:'business_909001',timing_ms:126,
    source:'member_token_field=lzbToken',
  });
  assert.equal(diagnostic({url:()=>origin+'/unrelated'},200),null);
});

test('does not treat data.token as a member session',()=>{
  for(const token of ['x'.repeat(768),'0123456789abcdef0123456789abcdef']){
    assert.equal(extractMemberSession({errcode:'0',data:{token}}),null);
  }
});

test('extracts only the verified lzbToken field and records its source',()=>{
  const token='0123456789abcdef0123456789abcdef';
  const session=extractMemberSession({errcode:'0',data:{token:'x'.repeat(768),lzbToken:token}});
  assert.deepEqual(session,{token,source:'official-sms-lzb'});
  assert.equal(extractMemberSession({errcode:'909001',data:{lzbToken:token}}),null);
});

test('rejects malformed member tokens without using the general session',()=>{
  for(const lzbToken of ['x'.repeat(32),'a'.repeat(31),'a'.repeat(33),null]){
    assert.equal(extractMemberSession({errcode:'0',data:{lzbToken,token:'x'.repeat(768)}}),null);
  }
});
