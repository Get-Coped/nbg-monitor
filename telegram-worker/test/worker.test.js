import test from "node:test";
import assert from "node:assert/strict";
import {webcrypto} from "node:crypto";
import {readFileSync} from "node:fs";
import {answer,authorized,bonds,changes,chunks,command,digestHex,findRows,handle,overview,terms} from "../src/worker.js";
globalThis.crypto ??= webcrypto;
const now=new Date("2026-09-14T12:00:00+04:00");
const pdf="https://nbg.gov.ge/fm/test.pdf";
function state() {
  const rows={
    "nbg:1":{source_id:"1",id:"123456789",issuer:"შპს ნიკორა ტრეიდი",isin:"",kind:"bond",documents:[pdf],date:"2026-09-01"},
    "nbg:2":{source_id:"2",id:"223456789",issuer:"შპს ნიკორა მენეჯმენტი",isin:"GE2700605613",kind:"bond",documents:[pdf],date:"2026-09-02"},
    "nbg:3":{source_id:"3",id:"323456789",issuer:"შპს თიბისი ლიზინგი",isin:"GE2700605614",kind:"bond",documents:[],date:"2026-09-03"},
    "nbg:4":{source_id:"4",id:"123456789",issuer:"შპს ნიკორა ტრეიდი",isin:"GE2700605615",kind:"share",documents:[pdf],date:"2026-09-04"},
  };
  return {v:6,rows,observed_rows:rows,documents:{[pdf]:{status:"baseline"}},last_ok:now.toISOString(),baseline_at:now.toISOString(),log:[],last_digest_at:now.toISOString()};
}
const ready={status:"ready",terms:{stage:"Indicative terms",fields:{
  "Placement agent":{value:"TBC Capital",page:2},"Currency and amount":{value:"USD up to 10 million",page:3},
  "Coupon":{value:"Fixed 7–7.25%",page:3}},restrictions:[{value:"Minimum GEL 500,000 equivalent",page:4}],pages_reviewed:60,total_pages:85}};

test("counts exclude shares and include bonds without ISIN",()=>{
  const s=state(),text=overview(s,now);
  assert.equal(bonds(s).length,3);
  assert.match(text,/Bond entries: <b>3/);
  assert.match(text,/Preliminary \/ awaiting ISIN: 1/);
  assert.match(text,/Share entries excluded: 1/);
  s.last_error="unverified";assert.match(overview(s,now),/not verified/);
});
test("natural questions, slash commands and Georgian/Latin aliases",()=>{
  const s=state();
  assert.equal(command("how many bonds?"),"overview");
  assert.equal(command("/changes@MyBot 7"),"changes:7");
  assert.equal(findRows(s,"Nikora").length,2);
  assert.equal(findRows(s,"TBC Leasing")[0].source_id,"3");
  assert.equal(findRows(s,"GE 2700605613")[0].source_id,"2");
  assert.equal(findRows(s,"nbg:1")[0].source_id,"1");
  assert.equal(findRows(s,"GE2700605615").length,0);
  const ambiguous=answer(s,command("/issuer Nikora"));
  assert.match(ambiguous.text,/Several issuers/);
  assert.equal(ambiguous.reply_markup.inline_keyboard.length,2);
});
test("terms preserve evidence and only uncached documents offer extraction",()=>{
  const s=state();
  assert.deepEqual(answer(s,"extract:1").job,{mode:"terms",row_key:"nbg:1"});
  s.documents[pdf]=ready;
  const r=answer(s,"extract:1");
  assert.equal(r.job,undefined);
  assert.match(r.text,/Fixed 7–7.25%/);
  assert.match(r.text,/#page=3/);
  assert.match(r.text,/not yet assigned/);
  assert.match(r.text,/not reliably extracted/);
  assert.match(r.text,/first 60 of 85/);
  assert.equal(answer(s,"extract:4").job,undefined);
  assert.equal(answer(s,"extract:https://evil.example/x.pdf").job,undefined);
});
test("digest does not mutate the scheduled digest cursor",()=>{
  const s=state(),before=structuredClone(s);
  assert.match(answer(s,"digest",now).text,/Recorded changes/);
  assert.deepEqual(s,before);
});
test("changes exclude technical noise and terms followups",()=>{
  const s=state(),row=s.rows["nbg:1"];
  s.log=["website_updated","terms_ready","prospectus_added"].map(kind=>({kind,row,ts:now.toISOString(),documents:[pdf]}));
  s.log.push({kind:"bond_added",row:s.rows["nbg:4"],ts:now.toISOString()});
  const r=changes(s,7,now);
  assert.match(r,/Preliminary publication/);
  assert.doesNotMatch(r,/website_updated|terms_ready|Bond entry added/);
  assert.match(r,/Reliable history begins/);
});
test("whole-line HTML chunks fit Telegram limits",()=>{
  const line="<b>"+ "🆕".repeat(900)+"</b>";
  const parts=chunks([line,line,line].join("\n"));
  assert.equal(parts.length,2);
  assert.ok(parts.every(p=>p.length<=3800 && p.endsWith("</b>")));
  const s=state();s.rows["nbg:1"].issuer="A <B> & Co";
  assert.match(terms(s,s.rows["nbg:1"]).text,/A &lt;B&gt; &amp; Co/);
});
test("real saved snapshot can render every bond and menu page",()=>{
  const s=JSON.parse(readFileSync(new URL("../../seen.json",import.meta.url)));
  // test/ -> telegram-worker/ -> repository root
  assert.equal(s.v,6);
  for(const row of bonds(s))assert.ok(chunks(terms(s,row).text).every(p=>p.length<=3800));
  for(let i=0;i<Math.ceil(bonds(s).length/6);i++)for(const line of answer(s,"list:"+i).reply_markup.inline_keyboard)for(const button of line)assert.ok(new TextEncoder().encode(button.callback_data).length<=64);
});

const env={TELEGRAM_TOKEN:"fake-token",TELEGRAM_CHAT_ID:"123",GITHUB_REPOSITORY:"Get-Coped/nbg-monitor"};
function update(text="/overview",id=10) {
  return {update_id:id,message:{text,chat:{id:123,type:"private"},from:{id:123,is_bot:false}}};
}
async function request(body,secret=true) {
  return new Request("https://example.workers.dev/telegram",{method:"POST",
    headers:secret?{"X-Telegram-Bot-Api-Secret-Token":await digestHex("nbg-webhook-v1:"+env.TELEGRAM_TOKEN)}:{},
    body:JSON.stringify(body)});
}
function network(s=state(),busy=false) {
  const calls=[];
  const net=async(url,opts={})=>{
    calls.push({url,opts});
    if(url.startsWith("https://raw.githubusercontent.com/"))return Response.json(s);
    if(url.startsWith("https://api.telegram.org/"))return Response.json({ok:true,result:{}});
    if(url.includes("/actions/runs?"))return Response.json({workflow_runs:busy?[{head_branch:"main",path:".github/workflows/monitor.yml"}]:[]});
    if(url.endsWith("/dispatches"))return new Response(null,{status:204});
    throw Error("Unexpected network target");
  };
  return {net,calls};
}
function memoryCache() {
  const saved=new Set();
  return {match:async k=>saved.has(k.url),put:async k=>{saved.add(k.url);}};
}
test("owner checks reject other users, chats and bots including callbacks",()=>{
  assert.ok(authorized(update(),env));
  const u=update();u.message.from.id=999;assert.ok(!authorized(u,env));
  u.message.from.id=123;u.message.from.is_bot=true;assert.ok(!authorized(u,env));
  const group={update_id:1,callback_query:{from:{id:999},message:{chat:{id:-10,type:"group"}}}};
  assert.ok(!authorized(group,{...env,TELEGRAM_CHAT_ID:"-10",TELEGRAM_USER_ID:"123"}));
  group.callback_query.from.id=123;
  assert.ok(authorized(group,{...env,TELEGRAM_CHAT_ID:"-10",TELEGRAM_USER_ID:"123"}));
  assert.ok(!authorized(group,{...env,TELEGRAM_CHAT_ID:"-10"}));
});
test("wrong webhook secret and unauthorized user make no outbound requests",async()=>{
  const {net,calls}=network();
  assert.equal((await handle(await request(update(),false),env,net,null)).status,403);
  const u=update();u.message.from.id=999;
  assert.equal((await handle(await request(u),env,net,null)).status,200);
  assert.equal(calls.length,0);
});
test("saved question fetches GitHub and replies only to configured chat",async()=>{
  const {net,calls}=network(),r=await handle(await request(update()),env,net,null);
  assert.equal(r.status,200);
  assert.equal(calls.length,2);
  assert.match(calls[0].url,/raw.githubusercontent.com/);
  const sent=JSON.parse(calls[1].opts.body);
  assert.equal(sent.chat_id,"123");assert.match(sent.text,/Bond entries: <b>3/);
  assert.ok(calls.every(c=>!c.url.includes("nbg.gov.ge")));
});
test("duplicate webhook update is acknowledged without duplicate reply",async()=>{
  const {net,calls}=network(),cache=memoryCache();
  await handle(await request(update()),env,net,cache);
  await handle(await request(update()),env,net,cache);
  assert.equal(calls.length,2);
});
test("cache outage does not cause a retry after successful delivery",async()=>{
  const {net,calls}=network();
  const cache={match:async()=>{throw Error("cache unavailable");},put:async()=>{throw Error("cache unavailable");}};
  assert.equal((await handle(await request(update()),env,net,cache)).status,200);
  assert.equal(calls.length,2);
});
test("failed delivery returns retryable status without storing receipt",async()=>{
  let stored=false;
  const net=async url=>url.startsWith("https://raw.")?Response.json(state()):new Response(null,{status:500});
  const cache={match:async()=>false,put:async()=>{stored=true;}};
  assert.equal((await handle(await request(update()),env,net,cache)).status,503);
  assert.equal(stored,false);
});
test("refresh dispatch uses an opaque ID and no private text in workflow inputs",async()=>{
  const {net,calls}=network();
  assert.equal((await handle(await request(update("/refresh")),{...env,GITHUB_DISPATCH_TOKEN:"fake-github"},net,null)).status,200);
  const dispatched=calls.find(c=>c.url.endsWith("/dispatches"));
  const payload=JSON.parse(dispatched.opts.body);
  assert.equal(payload.ref,"main");
  assert.match(payload.inputs.request_id,/^[a-f0-9]{24}$/);
  assert.deepEqual(Object.keys(payload.inputs).sort(),["mode","request_id","row_key"]);
  assert.equal(payload.inputs.mode,"refresh");
});
test("busy writer does not dispatch another job",async()=>{
  const {net,calls}=network(state(),true);
  await handle(await request(update("/refresh")),{...env,GITHUB_DISPATCH_TOKEN:"fake-github"},net,null);
  assert.ok(!calls.some(c=>c.url.endsWith("/dispatches")));
  assert.match(JSON.parse(calls.at(-1).opts.body).text,/already running/);
});
test("missing dispatch token remains an understandable saved-data service",async()=>{
  const {net,calls}=network();
  await handle(await request(update("/refresh")),env,net,null);
  assert.equal(calls.length,2);
  assert.match(JSON.parse(calls.at(-1).opts.body).text,/not configured yet/);
});

test('preliminary entries are directly selectable without an ISIN and appear first',()=>{
  const s=state();
  assert.equal(command('/preliminary'),'preliminary:0');
  const selected=answer(s,command('/preliminary Nikora'),now);
  assert.match(selected.text,/Preliminary stage/);
  assert.equal(selected.reply_markup.inline_keyboard[0][0].callback_data,'extract:1');
  assert.equal(answer(s,'list:0').reply_markup.inline_keyboard[0][0].callback_data,'terms:1');
  assert.equal(answer(s,'preliminary:0').reply_markup.inline_keyboard[0][0].callback_data,'terms:1');
});

test('preliminary cooldown gives a retry time instead of dispatching a useless job',()=>{
  const s=state();s.documents[pdf]={status:'retry',attempts:1,last_attempt:now.toISOString()};
  const waiting=answer(s,'extract:1',new Date(+now+14*60000));
  assert.equal(waiting.job,undefined);
  assert.match(waiting.text,/Next extraction attempt/);
  assert.deepEqual(answer(s,'extract:1',new Date(+now+15*60000)).job,{mode:'terms',row_key:'nbg:1'});
  assert.equal(answer(s,'extract:2',new Date(+now+15*60000)).job,undefined);
  s.documents[pdf].attempts=3;
  assert.equal(answer(s,'extract:1',new Date(+now+86400000)).job,undefined);
});

test('preliminary terms remain accessible after final documents and ISIN replace them',()=>{
  const s=state(),old=s.rows['nbg:1'];
  s.documents[pdf]={...ready,sha256:'a'.repeat(64),row:structuredClone(old),terms:{...ready.terms,stage:'Preliminary / indicative'}};
  old.isin='GE2700605621';old.documents=['https://nbg.gov.ge/fm/final.pdf'];
  s.rows['nbg:2'].documents=[];
  const menu=answer(s,'preliminary:0',now);
  assert.ok(menu.reply_markup.inline_keyboard.flat().some(b=>b.callback_data==='archive:'+ 'a'.repeat(16)));
  const result=answer(s,'preliminarysearch:Nikora',now);
  assert.match(result.text,/Historical preliminary terms/);
  assert.match(result.text,/Fixed 7–7.25%/);
  assert.equal(result.job,undefined);
});
