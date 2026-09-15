import test from "node:test";
import assert from "node:assert/strict";
import {webcrypto} from "node:crypto";
import {FriendsHub,handle,digestHex} from "../src/worker.js";
globalThis.crypto ??= webcrypto;
const env={TELEGRAM_TOKEN:"test-token",TELEGRAM_CHAT_ID:"123",GITHUB_DISPATCH_TOKEN:"test-github"};
class Storage {
  constructor(){this.data=new Map();this.alarm=null;}
  async get(k){return structuredClone(this.data.get(k));}
  async put(k,v){for(const [key,value] of typeof k==="string"?[[k,v]]:Object.entries(k))this.data.set(key,structuredClone(value));}
  async delete(k){for(const key of Array.isArray(k)?k:[k])this.data.delete(key);}
  async list({prefix="",limit=Infinity}={}){return new Map([...this.data].filter(([k])=>k.startsWith(prefix)).sort(([a],[b])=>a.localeCompare(b)).slice(0,limit).map(([k,v])=>[k,structuredClone(v)]));}
  async transaction(fn){return fn(this);}
  async setAlarm(n){this.alarm=n;}
  async deleteAlarm(){this.alarm=null;}
}
const hub=()=>new FriendsHub({storage:new Storage()},env);
async function add(h,chat,name="Friend"){await h.storage.put("member:"+chat,{chat,name,active:true,alerts:true});}
function update(chat,text,id=1){return {update_id:id,message:{text,chat:{id:Number(chat),type:"private"},from:{id:Number(chat),first_name:"Alex",is_bot:false}}};}
function network() {
  const calls=[];
  const net=async(url,opts={})=>{
    const body=opts.body?JSON.parse(opts.body):null;calls.push({url,body});
    if(url.includes("raw.githubusercontent.com"))return Response.json({v:6,last_ok:"2026-01-01T00:00:00Z",rows:{a:{kind:"bond",id:"issuer",source_id:"1",issuer:"Nikora",documents:[],isin:""}}});
    if(url.endsWith("/getMe"))return Response.json({ok:true,result:{username:"NbgTestBot"}});
    if(url.includes("api.telegram.org"))return Response.json({ok:true,result:{}});
    if(url.includes("/actions/runs?"))return Response.json({workflow_runs:[]});
    if(url.endsWith("/dispatches"))return new Response(null,{status:204});
    throw Error("Unexpected network request");
  };
  return {net,calls};
}
async function post(h,body,net,path="/telegram",valid=true){
  const secret=await digestHex((path==="/relay"?"nbg-relay-v1:":"nbg-webhook-v1:")+env.TELEGRAM_TOKEN);
  return handle(new Request("https://example.workers.dev"+path,{method:"POST",headers:{[path==="/relay"?"X-NBG-Relay-Secret":"X-Telegram-Bot-Api-Secret-Token"]:valid?secret:"invalid"},body:JSON.stringify(body)}),env,net,null,h);
}
const payload=(id="a",request_id)=>({id:id.repeat(64),text:"A new bond prospectus",...(request_id?{request_id}:{})});

test("one-use invitation opens the same menu and private saved queries",async()=>{
  const h=hub(),{net,calls}=network();
  assert.equal((await post(h,update("123","/invite"),net)).status,200);
  const text=calls.find(c=>c.url.endsWith("/sendMessage")).body.text;
  const token=text.match(/join_([a-f0-9]{40})/)[1];
  await post(h,update("456","/start join_"+token,2),net);
  assert.equal((await h.member("456")).alerts,true);
  assert.ok(calls.at(-1).body.reply_markup.inline_keyboard.length);
  await post(h,update("456","/overview",3),net);
  assert.equal(calls.at(-1).body.chat_id,"456");
  assert.match(calls.at(-1).body.text,/Bond entries: <b>1/);
  await post(h,update("789","/start join_"+token,4),net);
  assert.equal(await h.member("789"),undefined);
  assert.match(calls.at(-1).body.text,/invitation-only/);
  assert.ok(calls.every(c=>!c.url.includes("nbg.gov.ge")));
});

test("expired invitations and cancelled invitations grant no access",async()=>{
  const h=hub(),{net}=network(),token="1".repeat(40),key="invite:"+await digestHex(token);
  await h.storage.put(key,{expires:Date.now()-1});
  assert.equal(await h.join(token,"456","Friend"),false);
  await h.storage.put(key,{expires:Date.now()+60000});
  await h.manage("cancelinvites","123",true,"receipt",net);
  assert.equal(await h.join(token,"456","Friend"),false);
});

test("friend cannot invite, manage members, spoof a sender or use a group",async()=>{
  const h=hub(),{net,calls}=network();await add(h,"456");
  await post(h,update("456","/invite"),net);
  assert.match(calls.at(-1).body.text,/Only the owner/);
  await post(h,update("456","/friends",2),net);
  assert.match(calls.at(-1).body.text,/Only the owner/);
  const spoof=update("456","/overview",3);spoof.message.from.id=789;
  const group=update("456","/overview",4);group.message.chat.type="group";
  const count=calls.length;
  await post(h,spoof,net);await post(h,group,net);
  assert.equal(calls.length,count);
});

test("pause keeps queries available; resume restores broadcast subscription",async()=>{
  const h=hub(),{net,calls}=network();await add(h,"456");
  await post(h,update("456","/stop"),net);
  assert.equal((await h.member("456")).alerts,false);
  await post(h,update("456","/overview",2),net);
  assert.match(calls.at(-1).body.text,/Bond entries/);
  await post(h,update("456","/start",3),net);
  assert.equal((await h.member("456")).alerts,true);
});

test("owner revocation removes query and pending delivery access",async()=>{
  const h=hub(),{net,calls}=network();await add(h,"456");
  await h.enqueue(payload());
  await h.manage("revoke:456","123",true,"receipt",net);
  await post(h,update("456","/overview"),net);
  assert.equal(calls.length,0);
  await h.drain(net);
  assert.deepEqual(calls.map(c=>c.body.chat_id),["123"]);
});

test("private workflow inputs contain no friend identity or private text",async()=>{
  const h=hub(),{net,calls}=network();await add(h,"456","Secret friend");
  await post(h,update("456","/refresh"),net);
  const dispatch=calls.find(c=>c.url.endsWith("/dispatches")).body;
  assert.deepEqual(Object.keys(dispatch.inputs).sort(),["mode","private_reply","request_id","row_key"]);
  assert.equal(dispatch.inputs.private_reply,"true");
  assert.match(dispatch.inputs.request_id,/^[a-f0-9]{24}$/);
  assert.equal((await h.storage.get("request:"+dispatch.inputs.request_id)).chat,"456");
  assert.ok(!JSON.stringify(dispatch).includes("Secret friend"));
  await post(h,update("123","/refresh",2),net);
  assert.equal(calls.filter(c=>c.url.endsWith("/dispatches")).length,1);
});

test("daily job cap is shared by owner and friends and does not block saved queries",async()=>{
  const h=hub(),{net,calls}=network();await add(h,"456");
  for(let n=0;n<12;n++){const id=n.toString(16).padStart(24,"0");assert.equal((await h.reserve(id,n%2?"123":"456","terms")).ok,true);await h.release(id);}
  assert.equal((await h.reserve("f".repeat(24),"123","refresh")).ok,false);
  await post(h,update("456","/overview"),net);
  assert.match(calls.at(-1).body.text,/Bond entries/);
});

test("broadcast receipts survive restart and deliver each recipient once",async()=>{
  const h=hub(),{net,calls}=network();await add(h,"456");await add(h,"789");
  await h.storage.put("member:789",{...await h.member("789"),alerts:false});
  await h.enqueue(payload());
  const restarted=new FriendsHub({storage:h.storage},env);
  await restarted.enqueue(payload());
  await restarted.drain(net);
  await restarted.drain(net);
  assert.deepEqual(calls.map(c=>c.body.chat_id),["123","456"]);
});

test("private results go only to requester, even with notifications paused",async()=>{
  const h=hub(),{net,calls}=network();await add(h,"456");
  await h.manage("stop","456",false,"r",net);
  const id="1".repeat(24);await h.reserve(id,"456","terms");
  assert.equal((await post(h,payload("a",id),net,"/relay")).status,200);
  await h.drain(net);
  assert.deepEqual(calls.map(c=>c.body.chat_id),["456"]);
  assert.equal((await h.enqueue(payload("b","2".repeat(24)))).status,409);
});

test("relay rejects forged requests before any private storage or delivery",async()=>{
  const h=hub(),{net,calls}=network();
  assert.equal((await post(h,payload(),net,"/relay",false)).status,403);
  assert.equal(h.storage.data.size,0);assert.equal(calls.length,0);
  assert.equal((await h.enqueue({id:"bad",text:"hello"})).status,400);
});

test("one blocked friend cannot prevent delivery to others",async()=>{
  const h=hub();await add(h,"456");await add(h,"789");
  const sent=[],net=async(url,opts)=>{
    const body=JSON.parse(opts.body);
    if(body.chat_id==="456")return Response.json({ok:false,error_code:403},{status:403});
    sent.push(body.chat_id);return Response.json({ok:true,result:{}});
  };
  await h.enqueue(payload());await h.drain(net);
  assert.deepEqual(sent,["123","789"]);
  assert.equal((await h.member("456")).alerts,false);
});

test("transient failures preserve order without repeating successful recipients",async()=>{
  const h=hub();await add(h,"456");
  const now=Date.now(),sent=[];let fail=true;
  const net=async(url,opts)=>{
    const b=JSON.parse(opts.body);
    if(b.chat_id==="456" && fail)return Response.json({ok:false,error_code:429,parameters:{retry_after:120}},{status:429});
    sent.push([b.chat_id,b.text]);return Response.json({ok:true,result:{}});
  };
  await h.enqueue({...payload("a"),text:"first"});
  await h.enqueue({...payload("b"),text:"second"});
  await h.drain(net,now);
  await h.drain(net,now+2000);
  assert.deepEqual(sent,[["123","first"],["123","second"]]);
  fail=false;
  await h.drain(net,now+121000);await h.drain(net,now+123000);
  assert.deepEqual(sent,[["123","first"],["123","second"],["456","first"],["456","second"]]);
});

test("a repeated invitation webhook does not create another invitation",async()=>{
  const h=hub(),{net,calls}=network();
  await post(h,update("123","/invite"),net);
  await post(h,update("123","/invite"),net);
  assert.equal(calls.filter(c=>c.url.endsWith("/sendMessage")).length,1);
  assert.equal((await h.storage.list({prefix:"invite:"})).size,1);
});

test("private delivery cursors survive a failed later chunk",async()=>{
  const h=hub();await add(h,"456");
  const receipt=await digestHex("nbg-update:"+env.TELEGRAM_TOKEN+":123:1");
  await h.storage.put("update:"+receipt,{parts:["first","second"],sent:0,expires:Date.now()+10000});
  const sent=[];let fail=true;
  const net=async(url,opts)=>{
    const b=JSON.parse(opts.body);
    if(b.text==="second" && fail)return Response.json({ok:false,error_code:500},{status:500});
    sent.push(b.text);return Response.json({ok:true,result:{}});
  };
  assert.equal((await post(h,update("456","/overview"),net)).status,503);
  fail=false;
  assert.equal((await post(h,update("456","/overview"),net)).status,200);
  assert.deepEqual(sent,["first","second"]);
});
