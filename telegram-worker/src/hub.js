/** Private membership, routing and delivery receipts. Never stored in GitHub. */
import {handle, telegram, digestHex, esc, MENU} from "./worker.js";
const DAY=86400000, MAX_FRIENDS=50;
export class FriendsHub {
  constructor(ctx,env) {this.ctx=ctx;this.env=env;this.storage=ctx.storage;this.tail=Promise.resolve();}
  serial(fn) {const p=this.tail.then(fn);this.tail=p.catch(()=>{});return p;}
  fetch(request) {return this.serial(()=>handle(request,this.env,fetch,null,this));}
  alarm() {return this.serial(()=>this.drain());}
  owner() {return String(this.env.TELEGRAM_CHAT_ID);}
  async member(chat) {
    if(chat===this.owner())return {chat,name:"Owner",active:true,alerts:true,...await this.storage.get("member:"+chat)};
    return this.storage.get("member:"+chat);
  }
  async members() {
    const all=await this.storage.list({prefix:"member:"});
    return [...all.values()].filter(m=>m.chat!==this.owner() && m.active);
  }
  async access(update) {
    const m=update.callback_query?.message ?? update.message;
    const f=update.callback_query?.from ?? update.message?.from;
    if(!m || !f || f.is_bot || m.chat?.type!=="private" || String(f.id)!==String(m.chat.id))return false;
    return Boolean((await this.member(String(m.chat.id)))?.active);
  }
  async invitation(receipt,net) {
    const previous=await this.storage.get("invite-reply:"+receipt);
    if(previous)return previous.text;
    if((await this.members()).length>=MAX_FRIENDS)return "The bot currently supports 50 invited friends. Remove someone with /friends before inviting another.";
    const bot=await telegram(this.env,"getMe",{},net);
    if(!/^\w+bot$/i.test(bot.username || ""))throw Error("Bot username unavailable");
    const token=[...crypto.getRandomValues(new Uint8Array(20))].map(b=>b.toString(16).padStart(2,"0")).join("");
    const hash=await digestHex(token), expires=Date.now()+7*DAY;
    const text="Send this link to one friend:\nhttps://t.me/"+bot.username+"?start=join_"+token+
      "\n\nThey tap Start to get the same bond menu, alerts and daily digest. This invitation works once and expires in 7 days. Use /invite again for another friend, /cancelinvites to cancel unused links, or /friends to manage access.";
    await this.storage.put({["invite:"+hash]:{expires},["invite-reply:"+receipt]:{text,expires}});
    return text;
  }
  async join(token,chat,name) {
    if(!/^[a-f0-9]{40}$/.test(token))return false;
    const key="invite:"+await digestHex(token);
    const full=(await this.members()).length>=MAX_FRIENDS;
    return this.storage.transaction(async txn=>{
      const invite=await txn.get(key);
      if(!invite || invite.expires<Date.now())return false;
      if(invite.chat)return invite.chat===chat; // Retried Start from its original recipient.
      if(full)return false;
      await txn.put({[key]:{...invite,chat},["member:"+chat]:{chat,name:String(name || "Friend").slice(0,60),active:true,alerts:true}});
      return true;
    });
  }
  async manage(c,chat,isOwner,receipt,net) {
    if(c==="invite")return {text:isOwner?await this.invitation(receipt,net):"Only the owner can create invitations.",reply_markup:MENU};
    if(c==="cancelinvites") {
      if(!isOwner)return {text:"Only the owner can cancel invitations.",reply_markup:MENU};
      const invites=await this.storage.list({prefix:"invite:"});
      const keys=[...invites].filter(([,v])=>!v.chat).map(([k])=>k);
      if(keys.length)await this.storage.delete(keys);
      return {text:"Unused invitation links cancelled.",reply_markup:MENU};
    }
    if(c==="friends" || c.startsWith("revoke:")) {
      if(!isOwner)return {text:"Only the owner can manage access.",reply_markup:MENU};
      if(c.startsWith("revoke:")) {
        const target=c.slice(7), m=await this.member(target);
        if(target!==this.owner() && m)await this.storage.put("member:"+target,{...m,active:false,alerts:false});
      }
      const members=await this.members();
      return {text:"<b>Friends with access</b>\n"+(members.length?members.map(m=>"• "+esc(m.name)+(m.alerts?"":" — alerts paused")).join("\n"):"No friends have joined yet.")+
        "\n\nUse /invite to invite someone. Tap a name below to remove their access.",
        reply_markup:{inline_keyboard:[...members.map(m=>[{text:"Remove "+m.name,callback_data:"revoke:"+m.chat}]),[{text:"Main menu",callback_data:"menu"}]]}};
    }
    if(c==="stop" || c==="resume") {
      const m=await this.member(chat);
      if(m?.active)await this.storage.put("member:"+chat,{...m,alerts:c==="resume"});
      return {text:c==="stop"?"Alerts and the daily digest are paused. You can still use the menu. Send /start to resume notifications.":"Alerts and the daily digest are on. Choose an option below.",reply_markup:MENU};
    }
    return null;
  }
  async reserve(requestId,chat,mode) {
    const old=await this.storage.get("request:"+requestId);
    if(old)return {ok:false,text:"This request was already received. Its result will arrive here when ready."};
    const now=Date.now(), day=new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Tbilisi",year:"numeric",month:"2-digit",day:"2-digit"}).format(now);
    const gate=await this.storage.get("gate") || {};
    if(gate.until>now)return {ok:false,text:"A monitor request is already running. Please try again shortly. Saved lookups are still available."};
    const count=gate.day===day?gate.count:0;
    if(count>=12)return {ok:false,text:"Today's 12 shared on-demand jobs have been used. Saved lookups still work, and scheduled checks continue. Try again tomorrow."};
    await this.storage.put({gate:{day,count:count+1,until:now+10*60000,requestId},
      ["request:"+requestId]:{chat,mode,expires:now+30*DAY}});
    return {ok:true};
  }
  async release(requestId,refund=false) {
    const g=await this.storage.get("gate");
    if(g?.requestId===requestId)await this.storage.put("gate",{...g,until:0,count:Math.max(0,g.count-(refund?1:0))});
    if(refund)await this.storage.delete("request:"+requestId);
  }
  async enqueue(payload) {
    if(!/^[a-f0-9]{64}$/.test(payload.id || "") || typeof payload.text!=="string" || !payload.text || payload.text.length>3800 ||
       (payload.request_id && !/^[a-f0-9]{24}$/.test(payload.request_id)))return new Response("Invalid notification",{status:400});
    const key="delivery:"+payload.id;
    if(await this.storage.get(key))return Response.json({ok:true});
    let members;
    if(payload.request_id) {
      const request=await this.storage.get("request:"+payload.request_id);
      if(!request)return new Response("Unknown private request",{status:409});
      members=[await this.member(request.chat)].filter(m=>m?.active);
      await this.release(payload.request_id);
    }else members=[await this.member(this.owner()),...await this.members()].filter(m=>m.active && m.alerts);
    const queue=await this.storage.list({prefix:"queue:",limit:1001});
    if(queue.size>=1000)return new Response("Delivery queue full",{status:503});
    const sequence=(await this.storage.get("sequence") || 0)+1;
    // Alarm and queue are committed together, so an accepted message survives a restart.
    await this.storage.transaction(async txn=>{
      await txn.setAlarm(Date.now()+1000);
      await txn.put({sequence,[key]:{expires:Date.now()+30*DAY},
        ["queue:"+String(sequence).padStart(16,"0")]:{text:payload.text,request_id:payload.request_id || "",
          targets:members.map(m=>({chat:m.chat,attempts:0,due:0}))}});
    });
    return Response.json({ok:true});
  }
  async drain(net=fetch,now=Date.now()) {
    // Schedule before network I/O: timeouts or a process crash retain a retry alarm.
    await this.storage.setAlarm(now+60000);
    const queued=await this.storage.list({prefix:"queue:"}), used=new Map();
    let attempts=0, next=Infinity;
    for(const [key,item] of queued) {
      for(const target of item.targets) {
        if(target.done)continue;
        const member=await this.member(target.chat);
        if(!member?.active || (!item.request_id && !member.alerts)) {target.done=true;continue;}
        if(used.has(target.chat) || attempts>=10) {next=Math.min(next,used.get(target.chat) || now+1100);continue;}
        // Keep each recipient's chunks ordered even when their earlier delivery failed.
        used.set(target.chat,Math.max(target.due,now+1100));
        if(target.due>now) {next=Math.min(next,target.due);continue;}
        attempts++;
        try {
          await telegram(this.env,"sendMessage",{chat_id:target.chat,text:item.text,parse_mode:"HTML",disable_web_page_preview:true},net);
          target.done=true;
        }catch(e) {
          if(e.code===403) {
            await this.storage.put("member:"+target.chat,{...member,alerts:false});
            target.done=true;
          }else {
            target.attempts++;
            target.due=now+Math.max((e.retryAfter || 0)*1000,Math.min(3600000,30000*2**Math.min(target.attempts,7)));
            next=Math.min(next,target.due);
            used.set(target.chat,target.due);
          }
        }
        await this.storage.put(key,item);
      }
      if(item.targets.every(t=>t.done))await this.storage.delete(key);
      else await this.storage.put(key,item);
    }
    await this.clean(now);
    if(Number.isFinite(next))await this.storage.setAlarm(Math.max(Date.now()+1000,next));
    else await this.storage.deleteAlarm();
  }
  async clean(now=Date.now()) {
    if((await this.storage.get("cleaned") || 0)>now-DAY)return;
    for(const prefix of ["delivery:","request:","update:","invite:","invite-reply:"]) {
      const entries=await this.storage.list({prefix});
      const expired=[...entries].filter(([,v])=>v.expires<now).map(([k])=>k);
      if(expired.length)await this.storage.delete(expired);
    }
    await this.storage.put("cleaned",now);
  }
}
