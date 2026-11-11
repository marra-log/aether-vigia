"""Gera demo.html: o painel real com dados simulados, em um arquivo só.

Uso: python tools/build_demo.py demo.html
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
rng = np.random.default_rng(5)
W, H = 480, 270


def base(kind: str, night: bool = False) -> np.ndarray:
    tone = 62 if night else 150
    img = np.full((H, W, 3), tone, np.uint8)
    horizon = 120
    cv2.rectangle(img, (0, horizon), (W, H), (tone - 30,) * 3, -1)
    if kind == "portao":
        for x in range(150, 330, 16):
            cv2.line(img, (x, 60), (x, 215), (28, 28, 28), 3)
        cv2.rectangle(img, (150, 60), (330, 215), (28, 28, 28), 3)
        cv2.rectangle(img, (20, 40), (130, 215), (tone + 25,) * 3, -1)
        cv2.rectangle(img, (350, 40), (460, 215), (tone + 25,) * 3, -1)
    elif kind == "estacionamento":
        for x in range(30, W, 90):
            cv2.line(img, (x, 130), (x - 30, H), (225, 225, 225), 2)
        cv2.rectangle(img, (70, 165), (170, 225), (55, 55, 75), -1)
        cv2.rectangle(img, (270, 170), (370, 232), (120, 80, 50), -1)
        cv2.rectangle(img, (0, 30), (W, 95), (tone + 18,) * 3, -1)
    elif kind == "recepcao":
        cv2.rectangle(img, (130, 130), (350, 205), (105, 85, 65), -1)
        cv2.rectangle(img, (195, 40), (285, 100), (215, 215, 215), -1)
        cv2.circle(img, (70, 70), 26, (235, 235, 235), -1)
        cv2.rectangle(img, (390, 45), (465, 225), (80, 80, 80), -1)
    elif kind == "estoque":
        for y in (70, 125, 180):
            cv2.line(img, (20, y), (460, y), (40, 40, 40), 5)
        for x in range(30, 450, 50):
            for y in (32, 87, 142):
                h = int(rng.integers(20, 36))
                cv2.rectangle(img, (x, y + 36 - h), (x + 36, y + 36), (70, 115, 150), -1)
    else:
        cv2.rectangle(img, (0, 60), (W, 150), (tone + 12,) * 3, -1)
        cv2.line(img, (0, 150), (W, 240), (35, 35, 35), 3)
        for x in range(0, W, 60):
            cv2.line(img, (x, 60), (x, 150), (70, 70, 70), 2)
    return img


def osd(img: np.ndarray, name: str) -> np.ndarray:
    cv2.putText(img, name.upper(), (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def finish(img: np.ndarray) -> str:
    img = np.clip(img.astype(np.float32) + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    ok, data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 62])
    return "data:image/jpeg;base64," + base64.b64encode(data.tobytes()).decode()


def silhouette(img: np.ndarray, x: int, y: int, scale: float = 1.0) -> np.ndarray:
    head = int(13 * scale)
    cv2.circle(img, (x, y), head, (22, 22, 22), -1)
    cv2.rectangle(img, (x - int(16 * scale), y + head), (x + int(16 * scale), y + int(78 * scale)), (22, 22, 22), -1)
    cv2.rectangle(img, (x - int(14 * scale), y + int(78 * scale)), (x + int(14 * scale), y + int(122 * scale)), (30, 30, 30), -1)
    return img


CAMS = [(1, "Portão da frente", "portao"), (2, "Estacionamento", "estacionamento"),
        (3, "Recepção", "recepcao"), (4, "Estoque", "estoque"), (5, "Fundos", "fundos")]


def images() -> dict:
    out = {}
    for cid, name, kind in CAMS:
        out[f"/api/cameras/{cid}/thumb"] = finish(osd(base(kind), name))
    covered = np.full((H, W, 3), 58, np.uint8)
    covered = cv2.GaussianBlur(covered, (0, 0), 3)
    out["/api/cameras/4/thumb"] = finish(osd(covered.copy(), "Estoque"))
    out["/api/events/101/snapshot"] = finish(osd(covered.copy(), "Estoque"))
    out["/api/events/103/snapshot"] = finish(osd(silhouette(base("fundos", True), 245, 95), "Fundos"))
    out["/api/events/104/snapshot"] = finish(osd(base("estacionamento"), "Estacionamento"))
    blurred = cv2.GaussianBlur(base("recepcao"), (0, 0), 7)
    out["/api/events/105/snapshot"] = finish(osd(blurred, "Recepção"))
    return out


DEMO_JS = r"""
(function(){
const NOW=Math.floor(Date.now()/1000), STEP=900;
const IMAGES=__IMAGES__;
function rand(seed){let s=seed;return()=>{s=(s*1664525+1013904223)%4294967296;return s/4294967296}}
const cams=[
 {id:1,name:"Portão da frente",busy:3.2,seed:11},
 {id:2,name:"Estacionamento",busy:2.2,seed:23},
 {id:3,name:"Recepção",busy:4.5,seed:37},
 {id:4,name:"Estoque",busy:1.4,seed:53},
 {id:5,name:"Fundos",busy:.5,seed:71}];
function hourOf(t){const d=new Date(t*1000);return d.getHours()+d.getMinutes()/60}
function level(h,b){return (h>=8&&h<18)?b*(.75+.5*Math.sin((h-8)/10*Math.PI)):b*.05}
function build(c,step,count){
 const r=rand(c.seed),out=[],start=Math.floor(NOW/step)*step-(count-1)*step;
 for(let i=0;i<count;i++){
  const t=start+i*step,h=hourOf(t),exp=level(h,c.busy),n=.75+r()*.5;
  const p={t,uptime:1,fps:14.6+r()*.8,sharp:96+r()*6,brightness:120,motion:Math.max(exp*n,.01),flicker:0,reconnects:0,gaps:0,health:98+r()*2,persons:0,vehicles:0,
   low:Math.max(exp*.45,0),exp:exp,high:Math.max(exp*1.9,.18)};
  const ago=(NOW-t)/3600;
  if(c.id===4&&ago<.45){p.motion=0;p.sharp=9;p.health=40}
  if(c.id===5){ if(ago>7.2&&ago<7.8){p.motion=2.3;p.persons=1} if(ago<3.2&&ago>2.1&&i%2===0){p.uptime=.72;p.reconnects=2;p.gaps=6;p.fps=6+r()*5;p.health=62} if(ago<1.5&&ago>1.1){p.uptime=0;p.fps=0;p.health=0}}
  if(c.id===3&&ago>13&&ago<17){p.sharp=24+r()*5;p.health=78}
  if(c.id===2&&ago>4.8&&ago<5.4){p.motion=exp*4+3;}
  out.push(p);
 }
 return {series:out,start};
}
let events=[
 {id:107,camera_id:2,camera:"Estacionamento",type:"BEHAVIOR_SPIKE",kind:"comportamento",severity:"warning",status:"open",started_at:NOW-420,ended_at:null,title:"Movimento fora do normal",detail:"Há muito mais movimento do que esta câmera costuma ter neste horário.",advice:"Olhe a imagem para ver o que está acontecendo.",has_snapshot:true,acked:false,feedback:null,awaiting_review:true,reviewed_by:null},
 {id:108,camera_id:1,camera:"Portão da frente",type:"MANUAL",kind:"operador",severity:"critical",status:"closed",started_at:NOW-9400,ended_at:NOW-9400,title:"Anomalia apontada pelo operador",detail:"Portão aberto fora do horário de entrega.",advice:"Veja a observação e a imagem.",has_snapshot:true,acked:true,feedback:"real",awaiting_review:false,reviewed_by:"operator"},
 {id:101,camera_id:4,camera:"Estoque",type:"OBSTRUCTED",kind:"seguranca",severity:"critical",status:"open",started_at:NOW-1560,ended_at:null,title:"Câmera tampada ou bloqueada",detail:"A imagem perdeu quase todo o detalhe de repente.",advice:"Veja se algo cobriu a lente ou se houve sabotagem.",has_snapshot:true,acked:false,feedback:null},
 {id:102,camera_id:5,camera:"Fundos",type:"UNSTABLE",kind:"sinal",severity:"warning",status:"open",started_at:NOW-11200,ended_at:null,title:"Sinal oscilando",detail:"A câmera caiu 5 vezes na última hora.",advice:"Costuma ser cabo, fonte ou Wi-Fi fraco.",has_snapshot:false,acked:false,feedback:null},
 {id:103,camera_id:5,camera:"Fundos",type:"INTRUSION",kind:"seguranca",severity:"critical",status:"closed",started_at:NOW-27000,ended_at:NOW-26300,title:"Pessoa em área protegida",detail:"Uma pessoa foi detectada na área e no horário que você protegeu.",advice:"Olhe a imagem e acione quem for preciso.",has_snapshot:true,acked:false,feedback:null},
 {id:104,camera_id:2,camera:"Estacionamento",type:"BEHAVIOR_SPIKE",kind:"comportamento",severity:"warning",status:"closed",started_at:NOW-18300,ended_at:NOW-16900,title:"Movimento fora do normal",detail:"Há muito mais movimento do que esta câmera costuma ter neste horário.",advice:"Olhe a imagem para ver o que está acontecendo.",has_snapshot:true,acked:true,feedback:"real",reviewed_by:"operator"},
 {id:105,camera_id:3,camera:"Recepção",type:"BLUR",kind:"sinal",severity:"warning",status:"closed",started_at:NOW-60000,ended_at:NOW-47000,title:"Imagem desfocada",detail:"A nitidez caiu bem abaixo do normal desta câmera.",advice:"Limpe a lente e confira o foco.",has_snapshot:true,acked:true,feedback:"real"},
 {id:106,camera_id:5,camera:"Fundos",type:"OFFLINE",kind:"sinal",severity:"critical",status:"closed",started_at:NOW-5300,ended_at:NOW-3900,title:"Câmera fora do ar",detail:"Parei de receber imagem desta câmera.",advice:"Confira energia e cabo de rede da câmera.",has_snapshot:false,acked:true,feedback:"real"}
];
const armed={1:["22:00","06:00"],5:["22:00","06:00"]};
let recipients=[{id:1,name:"Dono",phone:"5531900000000",min_severity:"warning",quiet_start:"23:00",quiet_end:"06:00",active:true,can_command:true}];
let recent=[{id:10,kind:"alert",status:"review",created_at:NOW-420,sent_at:null,error:null,event_id:107},{id:9,kind:"alert",status:"sent",created_at:NOW-1500,sent_at:NOW-1500,error:null,event_id:101},{id:8,kind:"resolved",status:"sent",created_at:NOW-3900,sent_at:NOW-3900,error:null,event_id:106},{id:7,kind:"alert",status:"merged",created_at:NOW-5300,sent_at:null,error:null,event_id:106},{id:6,kind:"alert",status:"sent",created_at:NOW-11200,sent_at:NOW-11200,error:null,event_id:102},{id:5,kind:"digest",status:"sent",created_at:NOW-36000,sent_at:NOW-36000,error:null,event_id:null}];
let mute=0;
function camState(c){return "online"}
function pub(e){return JSON.parse(JSON.stringify(e))}
IMAGES["/api/events/107/snapshot"]=IMAGES["/api/events/104/snapshot"];IMAGES["/api/events/108/snapshot"]=IMAGES["/api/cameras/1/thumb"];
window.demoImage=p=>IMAGES[p]||"";
window.demoApi=async function(path,opts){
 opts=opts||{};const m=opts.method||"GET",body=opts.body;
 const [p,q]=path.split("?");const params=new URLSearchParams(q||"");
 if(p==="/api/me")return {role:window.VIGIA_DEMO_ROLE||"owner",review:true,review_minutes:10};
 if(p==="/api/overview"){
  const open=events.filter(e=>e.status==="open"||e.awaiting_review);
  return {site:"Comercial Exemplo",review_count:events.filter(e=>e.awaiting_review).length,now:Date.now()/1000,step:STEP,agent:{online:true,last_seen:NOW},events_24h:6,alerts_24h:5,mute_until:mute,open_events:open.map(pub),
   cameras:cams.map(c=>{const b=build(c,STEP,96),ce=open.filter(e=>e.camera_id===c.id);return {id:c.id,name:c.name,location:"",state:camState(c),health:97,learning:1,has_thumb:true,thumb_ts:NOW,open_events:ce.length,worst:ce[0]?ce[0].severity:null,series:b.series}})};
 }
 let x;
 if((x=p.match(/^\/api\/cameras\/(\d+)\/series$/))){
  const c=cams.find(k=>k.id==x[1]),h=+params.get("hours")||24,step=h<=24?300:1800,count=Math.floor(h*3600/step),b=build(c,step,count);
  return {camera:{id:c.id,name:c.name,location:"",state:"online",has_thumb:true,learning:1,threshold_mult:1},step,series:b.series,events:events.filter(e=>e.camera_id===c.id&&e.started_at>=b.start).map(pub)};
 }
 if(p==="/api/events"){
  const s=params.get("status"),k=params.get("kind");
  return {events:events.filter(e=>(!s||(s==="review"?e.awaiting_review:e.status===s))&&(!k||e.kind===k)).sort((a,b)=>b.started_at-a.started_at).map(pub)};
 }
 if((x=p.match(/^\/api\/events\/(\d+)\/feedback$/))){const e=events.find(k=>k.id==x[1]);e.feedback=body.feedback;e.acked=true;e.reviewed_by=window.VIGIA_DEMO_ROLE||"owner";if(e.awaiting_review){e.awaiting_review=false;if(body.feedback==="false"){e.status="closed";e.ended_at=Date.now()/1000}}return {ok:true}}
 if((x=p.match(/^\/api\/cameras\/(\d+)\/flag$/))){const c=cams.find(k=>k.id==x[1]),t=Date.now()/1000,id=Date.now();IMAGES["/api/events/"+id+"/snapshot"]=IMAGES["/api/cameras/"+c.id+"/thumb"];events.unshift({id,camera_id:c.id,camera:c.name,type:"MANUAL",kind:"operador",severity:body.severity,status:"closed",started_at:t,ended_at:t,title:"Anomalia apontada pelo operador",detail:body.note,advice:"Veja a observação e a imagem.",has_snapshot:true,acked:true,feedback:"real",awaiting_review:false,reviewed_by:window.VIGIA_DEMO_ROLE||"owner"});recent.unshift({id,kind:"alert",status:"sent",created_at:t,sent_at:t,error:null,event_id:id});return {ok:true}}
 if(p==="/api/cameras"&&m==="GET"){return {cameras:cams.map(c=>({id:c.id,name:c.name,location:"",enabled:true,state:"online",url_masked:"rtsp://•••@192.168.0."+(20+c.id)+":554/cam/realmonitor?channel=1&subtype=1",armed_start:(armed[c.id]||[null])[0],armed_end:(armed[c.id]||[null,null])[1],armed_days:[0,1,2,3,4,5,6],zones:[],loiter_seconds:180}))}}
 if(p.startsWith("/api/cameras")&&m!=="GET"){
  if(m==="POST"){const id=Math.max(...cams.map(c=>c.id))+1;cams.push({id,name:body.name,busy:2,seed:id*13});IMAGES["/api/cameras/"+id+"/thumb"]=IMAGES["/api/cameras/2/thumb"];if(body.armed_start)armed[id]=[body.armed_start,body.armed_end]}
  if(m==="PUT"){const c=cams.find(k=>p.endsWith("/"+k.id));c.name=body.name;if(body.armed_start)armed[c.id]=[body.armed_start,body.armed_end];else delete armed[c.id]}
  if(m==="DELETE"){const i=cams.findIndex(k=>p.endsWith("/"+k.id));cams.splice(i,1)}
  return {ok:true};
 }
 if(p==="/api/recipients"&&m==="GET"){return {recipients:recipients.map(pub),provider:{name:"meta",configured:true},mute_until:mute,recent:recent.map(pub)}}
 if(p==="/api/recipients"&&m==="POST"){const d=body.phone.replace(/\D/g,"");if(d.length<12)throw new Error("Use o número com DDI e DDD. Exemplo: 5531999998888");recipients.push({...body,phone:d,id:Date.now()});return {ok:true}}
 if(p.endsWith("/test")){recent.unshift({id:Date.now(),kind:"test",status:"sent",created_at:Date.now()/1000,sent_at:Date.now()/1000,error:null,event_id:null});return {ok:true}}
 if(p.startsWith("/api/recipients/")&&m==="DELETE"){recipients=recipients.filter(r=>!p.endsWith("/"+r.id));return {ok:true}}
 if(p==="/api/whatsapp/connection")return {supported:false};
 if(p==="/api/mute"){mute=body.hours?Date.now()/1000+body.hours*3600:0;return {until:mute}}
 return {};
};
})();
"""


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo.html")
    html = (ROOT / "server/app/static/index.html").read_text(encoding="utf-8")
    js = DEMO_JS.replace("__IMAGES__", json.dumps(images()))
    marker = '<script>\n"use strict";'
    assert marker in html
    html = html.replace(marker, f"<script>window.VIGIA_DEMO=true;</script>\n<script>{js}</script>\n{marker}", 1)
    html = html.replace("<title>Aether Vigia</title>", "<title>Aether Vigia: demonstração</title>")
    out.write_text(html, encoding="utf-8")
    print(out, round(out.stat().st_size / 1024), "KB")


if __name__ == "__main__":
    main()
