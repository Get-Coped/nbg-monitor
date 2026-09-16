
/** Invited-user Telegram queries. Ordinary lookups never call NBG. */
export {FriendsHub} from './hub.js';
export const MENU = {inline_keyboard: [
  [{text:"Preliminary terms",callback_data:"preliminary:0"}],
  [{text:"Overview",callback_data:"overview"},{text:"Recent changes",callback_data:"changes:7"}],
  [{text:"Find issuer",callback_data:"issuer"},{text:"Bond terms",callback_data:"list:0"}],
  [{text:"Digest now",callback_data:"digest"},{text:"Check NBG now",callback_data:"refresh"}]
]};
export const esc = s => String(s ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
const link = (u,label="Open prospectus") => '<a href="'+esc(u)+'">'+esc(label)+'</a>';
const short = (s,n=35) => s.length>n?s.slice(0,n-1)+"…":s;
export const bonds = s => Object.values(s.observed_rows ?? s.rows ?? {}).filter(r=>r.kind==="bond");
export function stamp(s) {
  const d=new Date(s.last_ok);
  if(!Number.isFinite(+d))return "unknown";
  return new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Tbilisi",day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",hour12:false}).format(d)+" Tbilisi";
}
export function freshness(s,now=new Date()) {
  return "Data checked: "+stamp(s)+(s.last_error || now-new Date(s.last_ok)>12*3600000
    ? "\nThe latest list is not verified; these are saved figures.":".");
}
export function overview(s,now=new Date()) {
  const r=bonds(s), shares=Object.values(s.observed_rows ?? s.rows).filter(r=>r.kind==="share").length;
  return ["<b>NBG bond overview</b>","Bond entries: <b>"+r.length+"</b>",
    "Issuers: "+new Set(r.map(r=>r.id)).size,"With ISIN: "+r.filter(r=>r.isin).length,
    "Preliminary / awaiting ISIN: "+r.filter(r=>!r.isin).length,"Share entries excluded: "+shares,
    "",freshness(s,now),"Counts describe the NBG page, not all outstanding bonds in Georgia."].join("\n");
}
const LABELS={prospectus_added:"Prospectus added",documents_replaced:"Document replaced",documents_removed:"Document removed",bond_added:"Bond entry added",bond_removed:"Bond entry removed",isin_assigned:"ISIN assigned",isin_changed:"ISIN updated"};
export function changes(s,days=7,now=new Date()) {
  days=Math.min(90,Math.max(1,parseInt(days)||7));
  const cutoff=+now-days*86400000, baseline=+new Date(s.baseline_at);
  const events=(s.log ?? []).filter(e=>e.row?.kind==="bond" && LABELS[e.kind] && +new Date(e.ts)>=Math.max(cutoff,baseline) && +new Date(e.ts)<=+now);
  const lines=["<b>Recorded changes — last "+days+" day(s)</b>"];
  if(baseline>cutoff)lines.push("Reliable history begins at the corrected baseline; earlier changes are unavailable.");
  if(!events.length)lines.push("No bond changes recorded in the available period.");
  for(const e of events.slice(-30).reverse()) {
    lines.push("• "+(!e.row.isin && ["prospectus_added","documents_replaced","bond_added"].includes(e.kind)?"Preliminary publication — ISIN pending":LABELS[e.kind])+": "+esc(e.row.issuer)+" — "+esc(e.row.isin || "ISIN pending"));
    for(const u of e.documents ?? [])lines.push(link(u));
  }
  if(events.length>30)lines.push("Showing the latest 30 of "+events.length+" events. Choose a shorter period.");
  if(Object.keys(s.pending_removals ?? {}).length || Object.keys(s.pending_documents ?? {}).length)lines.push("A possible removal is awaiting confirmation.");
  lines.push("",freshness(s,now));
  return lines.join("\n");
}
const ALIASES={nikora:"ნიკორა",rico:"რიკო",tegeta:"თეგეტა",tbc:"თიბისი","tbc leasing":"თიბისი ლიზინგი",alma:"ალმა",lopota:"ლოპოტა",mbc:"ემბისი",nova:"ნოვა",redix:"ჭავჭავაძის","bank of georgia":"საქართველოს ბანკი",ghg:"ჯანდაცვის"};
const norm = s => String(s ?? "").normalize("NFC").toLowerCase().replace(/[„“”"'.,-]/g," ").replace(/\s+/g," ").trim();
export function findRows(s,query) {
  const q=ALIASES[norm(query)] ?? norm(query);
  if(!q)return [];
  const isin=q.replaceAll(" ","").toUpperCase();
  return bonds(s).filter(r=>r.id===q || r.source_id===q || "nbg:"+r.source_id===q || (r.isin && r.isin===isin) || norm(r.issuer).includes(q));
}
export function listRows(s,rows=bonds(s),page=0,issuerId="",preliminary=false) {
  rows=[...rows].sort((a,b)=>Number(Boolean(a.isin))-Number(Boolean(b.isin)) || (b.date ?? "").localeCompare(a.date ?? "") || Number(b.source_id)-Number(a.source_id));
  const last=Math.max(0,Math.ceil(rows.length/6)-1);
  page=Math.max(0,Math.min(last,parseInt(page)||0));
  const selected=rows.slice(page*6,page*6+6), buttons=[], lines=[(preliminary?"<b>Preliminary terms — ISIN pending</b>":"<b>Select a bond</b>")+" — "+rows.length+" entries"];
  for(const r of selected) {
    lines.push("• "+esc(r.issuer)+"\n  "+esc(r.isin || "ISIN pending")+(r.date?" · "+esc(r.date):""));
    buttons.push([{text:short(r.issuer,24)+" · "+(r.isin || "pending"),callback_data:"terms:"+r.source_id}]);
  }
  const nav=[],prefix=preliminary?"preliminary:":issuerId?"issuerid:"+issuerId+":":"list:";
  if(page>0)nav.push({text:"← Previous",callback_data:prefix+(page-1)});
  if(page<last)nav.push({text:"Next →",callback_data:prefix+(page+1)});
  if(nav.length)buttons.push(nav);
  buttons.push([{text:"Main menu",callback_data:"menu"}]);
  lines.push("","Page "+(page+1)+"/"+(last+1),freshness(s));
  return {text:lines.join("\n"),reply_markup:{inline_keyboard:buttons}};
}
export function issuerResult(s,query) {
  const rows=findRows(s,query),ids=[...new Set(rows.map(r=>r.id))];
  if(!rows.length)return {text:"No matching bond issuer or ISIN. Try the Georgian name, identification code, Nikora, RICO or TBC Leasing.",reply_markup:MENU};
  if(ids.length===1)return listRows(s,rows,0,ids[0]);
  return {text:"<b>Choose the issuer</b>\nSeveral issuers match your search.",reply_markup:{inline_keyboard:ids.slice(0,20).map(id=>[{text:short(rows.find(r=>r.id===id).issuer,45),callback_data:"issuerid:"+id+":0"}])}};
}
export function extractionEligible(item,row,now=new Date()) {
  return item?.status!=="ready" && (item?.attempts || 0)<3 && (!item?.last_attempt || now-new Date(item.last_attempt)>=(row.isin?12*3600000:15*60000));
}
export function terms(s,row,now=new Date()) {
  const lines=["<b>"+esc(row.issuer)+"</b>","ISIN: "+esc(row.isin || "not yet assigned")], buttons=[];
  if(!row.isin)lines.push("Preliminary stage: indicative terms may change after bookbuilding. No ISIN is required to request extraction.");
  let missing=false;
  for(const u of row.documents) {
    const item=s.documents?.[u];lines.push("",link(u));
    if(item?.status!=="ready"){
      missing ||= extractionEligible(item,row,now);
      lines.push(esc(item?.reason || "Terms have not been extracted yet."));
      if((item?.attempts || 0)>=3)lines.push("Three extraction attempts used. Please review the linked document.");
      else if(!extractionEligible(item,row,now)) {
        const retryAt=new Date(+new Date(item.last_attempt)+(row.isin?12*3600000:15*60000));
        lines.push("Next extraction attempt: "+new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Tbilisi",day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",hour12:false}).format(retryAt)+" Tbilisi.");
      }
      continue;
    }
    const t=item.terms;lines.push(esc(t.stage));
    if(t.document_type==="Programme prospectus")lines.push(esc(t.note));
    if(t.fields?.["Programme amount"]) {const f=t.fields["Programme amount"];lines.push("Programme amount (not tranche size): "+esc(f.value)+" ("+link(u+"#page="+f.page,"p. "+f.page)+")");}
    for(const name of ["Placement agent","Currency and amount","Coupon","Tenor","Coupon payments","Issue date"]) {
      const f=t.fields?.[name];
      lines.push(name+": "+(f?esc(f.value)+" ("+link(u+"#page="+f.page,"p. "+f.page)+")":"not reliably extracted — review document"));
    }
    lines.push("Restrictions identified (review cited clauses):");
    if(!t.restrictions?.length)lines.push("Not reliably extracted; review the document.");
    for(const r of (t.restrictions ?? []).slice(0,3))lines.push("• "+esc(short(r.value,330))+" ("+link(u+"#page="+r.page,"p. "+r.page)+")");
    lines.push("Automatic extraction: first "+t.pages_reviewed+" of "+(t.total_pages ?? t.pages_reviewed)+" pages; restrictions are not exhaustive.");
  }
  if(!row.documents.length)lines.push("No prospectus link is listed for this bond.");
  if(missing)buttons.push([{text:row.isin?"Extract terms now":"Extract preliminary terms",callback_data:"extract:"+row.source_id}]);
  buttons.push([{text:"Main menu",callback_data:"menu"}]);lines.push("",freshness(s));
  return {text:lines.join("\n"),reply_markup:{inline_keyboard:buttons}};
}
export function preliminaryArchive(s) {
  const current=new Set(bonds(s).flatMap(r=>r.documents));
  return Object.entries(s.documents || {}).filter(([url,item])=>
    !current.has(url) && item.status==="ready" && item.row?.kind==="bond" && item.sha256 &&
    /preliminary|indicative/i.test(item.terms?.stage || ""));
}
export function preliminaryPage(s,page=0) {
  const result=listRows(s,bonds(s).filter(r=>!r.isin),page,"",true);
  const archive=preliminaryArchive(s).sort((a,b)=>(b[1].last_attempt || "").localeCompare(a[1].last_attempt || "")).slice(0,20);
  if(archive.length) {
    result.text+="\n\nSaved earlier preliminary terms (historical):";
    for(const [,item] of archive)result.reply_markup.inline_keyboard.splice(-1,0,[{
      text:"Earlier: "+short(item.row.issuer,35),callback_data:"archive:"+item.sha256.slice(0,16)}]);
  }
  return result;
}
export function command(text) {
  text=String(text ?? "").trim().slice(0,200);
  const m=text.match(/^\/(\w+)(?:@\w+)?(?:\s+([\s\S]*))?$/);
  if(m) {
    const c=m[1].toLowerCase(),arg=m[2] ?? "";
    return {preliminary:arg?"preliminarysearch:"+arg:"preliminary:0",start:"menu",help:"menu",menu:"menu",overview:"overview",bonds:"overview",digest:"digest",changes:"changes:"+(arg || "7"),issuer:"search:"+arg,terms:"termsearch:"+arg,refresh:"refresh"}[c] ?? "help";
  }
  if(/^(?:show )?(?:preliminary|indicative)(?: terms| bonds| prospectuses)?$/i.test(text))return "preliminary:0";
  if(/^(overview|how many bonds\??|bond count)$/i.test(text))return "overview";
  if(/^(digest|send (me )?(the )?digest)$/i.test(text))return "digest";
  if(/^(what changed this week\??|recent changes)$/i.test(text))return "changes:7";
  if(/^check (nbg )?now$/i.test(text))return "refresh";
  return "search:"+text.replace(/^show\s+/i,"").replace(/(?:'s)?\s+bonds$/i,"");
}
export function answer(s,c,now=new Date()) {
  if(s.v!==6)throw Error("Unsupported snapshot");
  const p=c.split(":");
  switch(p[0]) {
    case "preliminary":return preliminaryPage(s,p[1]);
    case "archive":{
      const entry=preliminaryArchive(s).find(([,item])=>item.sha256.slice(0,16)===p[1]);
      if(!entry)return {text:"Those earlier terms are not in the saved archive.",reply_markup:MENU};
      const [url,item]=entry,result=terms(s,{...item.row,documents:[url]},now);
      result.text="<b>Historical preliminary terms</b>\nThese are the earlier indicative terms, not the current final terms. The original NBG link may no longer be available.\n\n"+result.text;
      return result;
    }
    case "preliminarysearch":{
      const rows=findRows(s,p.slice(1).join(":")).filter(r=>!r.isin);
      if(rows.length===1)return terms(s,rows[0],now);
      if(!rows.length) {
        const q=ALIASES[norm(p.slice(1).join(":"))] ?? norm(p.slice(1).join(":"));
        const matches=preliminaryArchive(s).filter(([,item])=>norm(item.row.issuer).includes(q));
        if(matches.length===1)return answer(s,"archive:"+matches[0][1].sha256.slice(0,16),now);
      }
      return listRows(s,rows,0,"",true);
    }
    case "overview":return {text:overview(s,now),reply_markup:MENU};
    case "digest":return {text:overview(s,now)+"\n\n"+changes(s,1,now),reply_markup:MENU};
    case "changes":return {text:changes(s,p[1],now),reply_markup:MENU};
    case "issuer":return {text:"Send an issuer name, identification code or ISIN. For example: Nikora, RICO, TBC Leasing or ნიკორა.",reply_markup:{force_reply:true,selective:true}};
    case "search":return p.slice(1).join(":")?issuerResult(s,p.slice(1).join(":")):answer(s,"issuer",now);
    case "termsearch":{
      const q=p.slice(1).join(":");if(!q)return listRows(s);
      const r=findRows(s,q);return r.length===1?terms(s,r[0]):issuerResult(s,q);
    }
    case "list":return listRows(s,bonds(s),p[1]);
    case "issuerid":return listRows(s,bonds(s).filter(r=>r.id===p[1]),p[2],p[1]);
    case "terms":{
      const r=bonds(s).find(r=>r.source_id===p[1]);
      return r?terms(s,r):{text:"That bond is not in the latest saved list. Open Bond terms to select a current entry.",reply_markup:MENU};
    }
    case "refresh":return {job:{mode:"refresh",row_key:""}};
    case "extract":{
      const r=bonds(s).find(r=>r.source_id===p[1]);
      if(!r?.documents.length)return {text:"No current prospectus found for that entry.",reply_markup:MENU};
      if(!r.documents.some(u=>extractionEligible(s.documents?.[u],r,now)))return terms(s,r,now);
      return {job:{mode:"terms",row_key:"nbg:"+r.source_id}};
    }
    default:return {text:"<b>NBG bond assistant</b>\nUse /preliminary or /preliminary RICO for indicative terms before ISIN assignment. Use the buttons, /issuer Nikora, /terms GE2700605373, or /changes 7.\n\nOrdinary lookups use saved data. Check NBG now requests a fresh check. Extract terms now downloads the selected prospectus.",reply_markup:MENU};
  }
}
export function chunks(text,limit=3800) {
  const out=[];let part="";
  for(const line of text.split("\n")) {
    if(line.length>limit)throw Error("Notification line too long");
    const next=part?part+"\n"+line:line;
    if(next.length>limit){out.push(part);part=line;}else part=next;
  }
  if(part)out.push(part);return out;
}
export async function digestHex(s) {
  const b=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,"0")).join("");
}
export function authorized(update,env) {
  const m=update.callback_query?.message ?? update.message;
  const from=update.callback_query?.from ?? update.message?.from;
  const owner=String(env.TELEGRAM_USER_ID || env.TELEGRAM_CHAT_ID || "");
  return m && from && !from.is_bot && String(m.chat?.id)===String(env.TELEGRAM_CHAT_ID) &&
    /^\d+$/.test(owner) && String(from.id)===owner &&
    (m.chat.type==="private" || Boolean(env.TELEGRAM_USER_ID));
}
export async function telegram(env,method,payload,net) {
  const r=await net("https://api.telegram.org/bot"+env.TELEGRAM_TOKEN+"/"+method,{
    method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),
    signal:AbortSignal.timeout(15000)});
  const d=await r.json();
  if(!r.ok || !d.ok) {const e=Error("Telegram request failed");e.code=d.error_code || r.status;e.retryAfter=d.parameters?.retry_after;throw e;}
  return d.result;
}
async function snapshot(env,net) {
  const repo=env.GITHUB_REPOSITORY || "Get-Coped/nbg-monitor";
  if(!/^[\w.-]+\/[\w.-]+$/.test(repo))throw Error("Invalid repository");
  const r=await net("https://raw.githubusercontent.com/"+repo+"/main/seen.json",{
    cf:{cacheTtl:30,cacheEverything:true},signal:AbortSignal.timeout(15000)});
  if(!r.ok)throw Error("Saved data unavailable");
  const s=await r.json();
  if(s.v!==6 || !s.rows || !s.last_ok)throw Error("Saved data not ready");
  return s;
}
async function dispatch(env,job,requestId,net,hub=null,chat="") {
  if(!env.GITHUB_DISPATCH_TOKEN)return "Fresh checks and first-time PDF extraction are not configured yet. Saved lookups remain available.";
  const root="https://api.github.com/repos/"+(env.GITHUB_REPOSITORY || "Get-Coped/nbg-monitor");
  const headers={Authorization:"Bearer "+env.GITHUB_DISPATCH_TOKEN,Accept:"application/vnd.github+json","Content-Type":"application/json","User-Agent":"NBG-Telegram-Requests","X-GitHub-Api-Version":"2022-11-28"};
  // Avoid replacing a pending writer in GitHub's single-pending concurrency group.
  for(const status of ["queued","in_progress"]) {
    const r=await net(root+"/actions/runs?status="+status+"&per_page=30",{headers,signal:AbortSignal.timeout(10000)});
    if(!r.ok)throw Error("Cannot check job status");
    const runs=(await r.json()).workflow_runs || [];
    if(runs.some(r=>r.head_branch==="main" && /\/(monitor|on-demand)\.yml$/.test(r.path)))return "A monitor request is already running. Please try again shortly. Saved lookups are still available.";
  }
  if(hub) {
    const allowed=await hub.reserve(requestId,chat,job.mode);
    if(!allowed.ok)return allowed.text;
  }
  const r=await net(root+"/actions/workflows/on-demand.yml/dispatches",{method:"POST",headers,
    body:JSON.stringify({ref:"main",inputs:{...job,request_id:requestId,...(hub?{private_reply:"true"}:{})}}),signal:AbortSignal.timeout(10000)});
  if(!r.ok) {if(hub)await hub.release(requestId,true);throw Error("Could not queue request");}
  return job.mode==="refresh"?"Check requested. I’ll send the result when it finishes; GitHub may take a minute to start.":"Extraction requested. I’ll send the selected prospectus terms when ready.";
}
export async function handle(request,env,net=fetch,cache=globalThis.caches?.default,hub=null) {
  const url=new URL(request.url);
  if(url.pathname==="/health" && request.method==="GET") {
    const configured=Boolean(env.TELEGRAM_TOKEN && env.TELEGRAM_CHAT_ID);
    return Response.json({service:"nbg-telegram-requests",configured,jobs_enabled:Boolean(env.GITHUB_DISPATCH_TOKEN),
      preliminary_enabled:true,friends_enabled:Boolean(env.FRIENDS),relay_version:env.FRIENDS?1:0},{status:configured?200:503});
  }
  if(!["/telegram","/relay"].includes(url.pathname) || request.method!=="POST")return new Response("Not found",{status:404});
  if(!env.TELEGRAM_TOKEN || !env.TELEGRAM_CHAT_ID)return new Response("Not configured",{status:503});
  const relay=url.pathname==="/relay";
  const expected=await digestHex((relay?"nbg-relay-v1:":"nbg-webhook-v1:")+env.TELEGRAM_TOKEN);
  if(request.headers.get(relay?"X-NBG-Relay-Secret":"X-Telegram-Bot-Api-Secret-Token")!==expected)return new Response("Forbidden",{status:403});
  if(Number(request.headers.get("Content-Length")||0)>65536)return new Response("Too large",{status:413});
  if(env.FRIENDS && !hub) {
    try {return await env.FRIENDS.get(env.FRIENDS.idFromName("friends-v1")).fetch(request);}
    catch {return new Response("Temporary request failure",{status:503});}
  }
  const raw=await request.text();if(raw.length>65536)return new Response("Too large",{status:413});
  let update;try{update=JSON.parse(raw);}catch{return new Response("Bad JSON",{status:400});}
  if(relay) {
    if(!hub)return new Response("Relay unavailable",{status:503});
    try {return await hub.enqueue(update);}catch{return new Response("Temporary delivery failure",{status:503});}
  }
  if(!Number.isSafeInteger(update.update_id))return new Response("Bad update",{status:400});
  const m=update.callback_query?.message ?? update.message, from=update.callback_query?.from ?? update.message?.from;
  const isOwner=Boolean(authorized(update,env)), chat=String(m?.chat?.id ?? "");
  const privateChat=m?.chat?.type==="private" && from && !from.is_bot && String(from.id)===chat;
  const receipt=await digestHex("nbg-update:"+env.TELEGRAM_TOKEN+":"+env.TELEGRAM_CHAT_ID+":"+update.update_id);
  const text=String(update.message?.text || "");
  const start=text.match(/^\/start(?:@\w+)?(?:\s+join_([a-f0-9]{40}))?\s*$/);
  let joined=false;
  if(hub && privateChat && start?.[1])joined=await hub.join(start[1],chat,from.first_name);
  if(!isOwner && !(hub && await hub.access(update))) {
    // A friend opening an invalid/expired invite gets a useful answer, never data.
    if(hub && privateChat && /^\/start(?:@\w+)?(?:\s|$)/.test(text)) {
      const key="update:"+receipt;
      if(!await hub.storage.get(key)) {
        try {
          await telegram(env,"sendMessage",{chat_id:chat,text:"This bot is invitation-only. Ask its owner for a new /invite link, then open it and tap Start."},net);
          await hub.storage.put(key,{expires:Date.now()+86400000});
        }catch{return new Response("Temporary request failure",{status:503});}
      }
    }
    return new Response("OK");
  }
  const key=new Request("https://nbg-receipts.invalid/"+receipt);
  try{if(!hub && cache && await cache.match(key))return new Response("OK");}catch{}
  try {
    await hub?.clean();
    let delivery=hub?await hub.storage.get("update:"+receipt):null;
    if(delivery?.done)return new Response("OK");
    if(update.callback_query) {
      try{await telegram(env,"answerCallbackQuery",{callback_query_id:update.callback_query.id},net);}catch{}
    }
    if(!delivery) {
      let c=String(update.callback_query?.data ?? command(text || "/help"));
      const admin=text.match(/^\/(invite|friends|cancelinvites|stop)(?:@\w+)?\s*$/);
      if(admin)c=admin[1];
      if(start && !start[1])c="resume";
      let result=hub?await hub.manage(c,chat,isOwner,receipt,net):null;
      if(joined)result={text:"Welcome! You now have the bond menu, publication alerts and daily digest. Use /stop to pause notifications and /start to resume them.",reply_markup:MENU};
      if(!result) {
        const s=await snapshot(env,net);
        result=answer(s,c);
        if(result.job) {
          // A recent check can answer everyone immediately without another Action.
          if(hub && result.job.mode==="refresh" && Date.now()-new Date(s.last_ok)<15*60000)
            result={text:"A check was made recently. Reusing it to avoid repeated NBG requests.\n\n"+overview(s),reply_markup:MENU};
          else result={text:await dispatch(env,result.job,receipt.slice(0,24),net,hub,chat),reply_markup:MENU};
        }
      }
      delivery={parts:chunks(result.text),reply_markup:result.reply_markup,sent:0,expires:Date.now()+7*86400000};
      if(hub)await hub.storage.put("update:"+receipt,delivery);
    }
    for(let i=delivery.sent;i<delivery.parts.length;i++) {
      await telegram(env,"sendMessage",{chat_id:chat,text:delivery.parts[i],parse_mode:"HTML",disable_web_page_preview:true,
        ...(i===delivery.parts.length-1?{reply_markup:delivery.reply_markup}:{})},net);
      delivery.sent=i+1;
      if(hub)await hub.storage.put("update:"+receipt,delivery);
    }
    if(hub)await hub.storage.put("update:"+receipt,{done:true,expires:Date.now()+7*86400000});
    else try{if(cache)await cache.put(key,new Response("done",{headers:{"Cache-Control":"max-age=86400"}}));}catch{}
    return new Response("OK");
  }catch {
    // No request bodies, tokens or private messages enter public logs.
    return new Response("Temporary request failure",{status:503});
  }
}
export default {fetch(request,env){return handle(request,env);}};
