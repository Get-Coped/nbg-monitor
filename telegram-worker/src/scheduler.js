/**
 * Reliable timing. Cloudflare cron wakes this every two minutes in Tbilisi business
 * hours, at 20:45 and through the 21:00 hour. GitHub's own schedule runs hours late,
 * so it is only a backup; this module starts the same GitHub workflow on time.
 *
 * Fast path: one NBG page request per poll, reduced to a fingerprint of the listed
 * securities. Only a changed fingerprint starts the full check (parsing, PDF terms,
 * Telegram alerts), so an ordinary day costs no GitHub runs and no state commits.
 */
import {telegram, digestHex} from "./worker.js";

export const NBG_URL="https://nbg.gov.ge/supervision/public-companies";
const AGENT="NBG-Publication-Monitor/6 (+https://github.com/Get-Coped/nbg-monitor)";
const MIN=60000, KEY="scheduler";
export const POLL_FAILS_BEFORE_FALLBACK=5;   // ten minutes of failed polls
export const FALLBACK_EVERY=10*MIN;          // full GitHub check cadence while polling is blocked
export const DAILY_CHANGE_CAP=40;            // beyond this, a flapping page is throttled
export const THROTTLED_EVERY=30*MIN;
export const MAX_TRIES=3;

/** Tbilisi is UTC+4 all year. */
export function tbilisi(now) {
  const d=new Date(+now+4*3600000);
  return {day:d.toISOString().slice(0,10),weekday:d.getUTCDay(),hour:d.getUTCHours(),minute:d.getUTCMinutes()};
}
export function slot(now) {
  const t=tbilisi(now);
  if(t.hour===21)return "digest";
  if(t.hour===20 && t.minute>=45)return "evening";
  if(t.weekday>=1 && t.weekday<=5 && t.hour>=9 && t.hour<19)return "poll";
  return "idle";
}
const clock = ms => ms?new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Tbilisi",day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(ms))+" Tbilisi":"never";

/** The same PDF whatever cache suffix or tracking parameter this render carries. */
function documentKey(value) {
  let text=String(value ?? "").normalize("NFC").trim();
  const q=text.indexOf("?");
  let query="";
  if(q>=0) {query=text.slice(q+1);text=text.slice(0,q);}
  try{text=decodeURIComponent(text);}catch{}
  const kept=query.split("&").filter(p=>p && !/^(v|fbclid|utm_[^=]*)(=|$)/i.test(p)).sort();
  return text.normalize("NFC")+(kept.length?"?"+kept.join("&"):"");
}
/** Identity of what NBG lists: record, issuer code, instrument type, ISIN, documents.
 *  Issuer spelling and status dates are deliberately excluded; they are not alerts. */
export async function fingerprint(html) {
  const open=String(html).match(/<script[^>]*id="__NEXT_DATA__"[^>]*>/);
  if(!open)throw Error("unreadable page");
  const start=open.index+open[0].length, end=html.indexOf("</script>",start);
  let records;
  try{records=JSON.parse(html.slice(start,end)).props.initialProps.pageProps.organizations;}catch{throw Error("unreadable page");}
  if(!Array.isArray(records) || !records.length)throw Error("empty securities list");
  const rows=records.map(r=>{
    if(!Array.isArray(r.approvedDocuments))throw Error("unreadable page");
    return [String(r.id),String(r.identificationCode ?? "").trim(),String(r.licenseOrOrderNumber ?? "").trim(),
      String(r.licenseType ?? "").replace(/\s+/g,"").toUpperCase(),r.approvedDocuments.map(documentKey).sort()];
  }).sort((a,b)=>a[0].localeCompare(b[0]));
  return digestHex(JSON.stringify(rows));
}

const repoRoot = env => "https://api.github.com/repos/"+(env.GITHUB_REPOSITORY || "Get-Coped/nbg-monitor");
const github = env => ({Authorization:"Bearer "+env.GITHUB_DISPATCH_TOKEN,Accept:"application/vnd.github+json","Content-Type":"application/json","User-Agent":"NBG-Telegram-Requests","X-GitHub-Api-Version":"2022-11-28"});

async function tell(env,net,text) {
  // Operational notices go to the owner only, and never fail a tick.
  try{await telegram(env,"sendMessage",{chat_id:env.TELEGRAM_CHAT_ID,text,parse_mode:"HTML",disable_web_page_preview:true},net);}catch{}
}

/** Start monitor.yml. Returns "started", "busy" (another job is waiting) or "failed". */
async function start(env,net,s,mode,now) {
  if(!env.GITHUB_DISPATCH_TOKEN)return "failed";
  try {
    // GitHub keeps one waiting job per concurrency group; never displace it.
    for(const status of ["queued","pending"]) {
      const r=await net(repoRoot(env)+"/actions/runs?status="+status+"&per_page=30",{headers:github(env),signal:AbortSignal.timeout(10000)});
      if(!r.ok)throw Error("HTTP "+r.status);
      if(((await r.json()).workflow_runs || []).some(x=>x.head_branch==="main" && /\/(monitor|on-demand)\.yml$/.test(x.path)))return "busy";
    }
    const r=await net(repoRoot(env)+"/actions/workflows/monitor.yml/dispatches",{method:"POST",headers:github(env),
      body:JSON.stringify({ref:"main",inputs:{mode}}),signal:AbortSignal.timeout(10000)});
    if(!r.ok)throw Error("HTTP "+r.status);
  }catch(e) {
    s.github_fails=(s.github_fails || 0)+1;
    s.github_error=String(e.message || "request failed").slice(0,40);
    if(s.github_fails===3)await tell(env,net,"<b>NBG monitor: GitHub is not starting jobs</b>\nThree attempts failed ("+s.github_error+"). If the access token (ON_DEMAND_GITHUB_TOKEN) expired, renew it and rerun the deployment workflow. GitHub's own delayed schedule still runs as a backup.");
    return "failed";
  }
  s.github_fails=0;s.github_error="";s.last_start=now;
  return "started";
}

/** "success", "running", "failed" or "unknown" for jobs of this kind started since `since`. */
async function outcome(env,net,title,since) {
  try {
    const r=await net(repoRoot(env)+"/actions/workflows/monitor.yml/runs?event=workflow_dispatch&per_page=15",{headers:github(env),signal:AbortSignal.timeout(10000)});
    if(!r.ok)return "unknown";
    const runs=((await r.json()).workflow_runs || []).filter(x=>x.display_title===title && +new Date(x.created_at)>=since-30000);
    if(runs.some(x=>x.conclusion==="success"))return "success";
    if(runs.some(x=>x.status!=="completed"))return "running";
    return runs.length?"failed":"unknown";
  }catch{return "unknown";}
}

async function requestCheck(env,net,s,now) {
  const day=tbilisi(now).day;
  if(s.count_day!==day){s.count_day=day;s.count=0;s.cap_told=false;}
  if(s.count>=DAILY_CHANGE_CAP) {
    if(!s.cap_told) {
      s.cap_told=true;
      await tell(env,net,"<b>NBG monitor: unusually many page changes today</b>\nFull checks are limited to one every 30 minutes for the rest of the day. Alerts still arrive, a little later.");
    }
    if(now-(s.last_start || 0)<THROTTLED_EVERY)return "busy";
  }
  const result=await start(env,net,s,"check",now);
  if(result==="started")s.count++;
  return result;
}

async function followUp(env,net,s,now) {
  // A detected change is only finished when the full check has succeeded.
  const p=s.pending;
  if(!p)return;
  const state=await outcome(env,net,"NBG check",p.at);
  if(state==="success"){s.pending=null;return;}
  if(state==="running" || now-p.at<(state==="failed"?MIN:8*MIN))return;
  if(p.tries>=MAX_TRIES) {
    s.pending=null;
    await tell(env,net,"<b>NBG monitor: a page change could not be processed</b>\nA change on the NBG page was detected at "+clock(p.first)+", but the full check did not complete after "+MAX_TRIES+" attempts. Please open the NBG page; the backup schedule will retry later.\n"+NBG_URL);
    return;
  }
  if(await start(env,net,s,"check",now)==="started"){p.tries++;p.at=now;}
}

async function poll(env,net,s,now) {
  await followUp(env,net,s,now);
  let print;
  try {
    const r=await net(NBG_URL,{headers:{"User-Agent":AGENT,Accept:"text/html"},redirect:"manual",signal:AbortSignal.timeout(20000)});
    if(r.status!==200)throw Error("HTTP "+r.status);
    print=await fingerprint(await r.text());
  }catch(e) {
    s.fails=(s.fails || 0)+1;
    s.error=e.name==="TimeoutError"?"timeout":String(e.message || "request failed").slice(0,40);
    if(s.fails===POLL_FAILS_BEFORE_FALLBACK) {
      s.fallback=true;
      await tell(env,net,"<b>NBG monitor: fast polling is not getting through</b>\nThe two-minute poll has failed "+s.fails+" times in a row ("+s.error+"). Until it recovers, a full check runs every 10 minutes in business hours instead, so alerts may be up to about 10 minutes late.");
    }
    if(s.fallback && now-(s.last_start || 0)>=FALLBACK_EVERY)await start(env,net,s,"check",now);
    return;
  }
  if(s.fallback) {
    s.fallback=false;
    await tell(env,net,"<b>NBG monitor: fast polling recovered</b>\nTwo-minute polling is working again.");
  }
  s.fails=0;s.error="";s.polled=now;
  if(print===s.print)return;
  // Remember the new fingerprint only once the full check is actually on its way.
  if(await requestCheck(env,net,s,now)==="started") {
    s.print=print;s.changed=now;
    s.pending={at:now,first:now,tries:1};
  }
}

async function digest(env,net,s,now) {
  const day=tbilisi(now).day;
  if(s.digest?.day!==day)s.digest={day,tries:0,at:0,done:false};
  const d=s.digest;
  if(d.done)return;
  if(d.tries) {
    const state=await outcome(env,net,"NBG digest",d.at);
    if(state==="success"){d.done=true;d.sent=now;return;}
    if(state==="running" || now-d.at<(state==="failed"?MIN:9*MIN))return;
    if(d.tries>=MAX_TRIES) {
      d.done=true;
      await tell(env,net,"<b>NBG monitor: tonight's digest could not be confirmed</b>\nThree attempts to send the 21:00 digest did not complete. Use Digest now in the menu; GitHub's backup schedule will also retry later tonight.");
      return;
    }
  }
  if(await start(env,net,s,"digest",now)==="started"){d.tries++;d.at=now;}
}

/** One cron wake-up. `hub` supplies durable storage and secrets. */
export async function tick(hub,now=Date.now(),net=fetch) {
  now=+now;
  const s=await hub.storage.get(KEY) || {};
  const what=slot(now);
  if(what==="poll")await poll(hub.env,net,s,now);
  else if(what==="evening") {
    // Fresh figures for the digest, and the one daily check on weekends.
    if(s.evening!==tbilisi(now).day && await start(hub.env,net,s,"check",now)==="started")s.evening=tbilisi(now).day;
  }
  else if(what==="digest")await digest(hub.env,net,s,now);
  s.ticked=now;
  await hub.storage.put(KEY,s);
  return what;
}

/** Owner-facing summary for /status. */
export async function status(hub,now=Date.now()) {
  const s=await hub.storage.get(KEY);
  if(!s?.ticked)return "<b>Monitor status</b>\nThe scheduler has not run yet. It starts with the next two-minute poll in business hours (Mon–Fri 09:00–19:00 Tbilisi).";
  const lines=["<b>Monitor status</b>",
    "Fast polling: "+(s.fallback?"blocked ("+(s.error || "error")+") — full check every 10 minutes instead":s.fails?"retrying ("+s.fails+" failed, "+s.error+")":"working"),
    "Last successful poll: "+clock(s.polled),
    "Last page change detected: "+clock(s.changed),
    "Last GitHub job started: "+clock(s.last_start)+(s.github_fails?" — "+s.github_fails+" recent failure(s): "+s.github_error:""),
    "Last digest confirmed: "+clock(s.digest?.sent)];
  if(s.pending)lines.push("A detected change is being processed now.");
  if(now-s.ticked>26*3600000)lines.push("Warning: the scheduler has not run for over a day.");
  lines.push("","Polls every 2 minutes, Mon–Fri 09:00–19:00 Tbilisi. One check daily at 20:45. Digest at 21:00.");
  return lines.join("\n");
}
