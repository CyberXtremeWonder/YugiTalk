// 多任务对话浏览器验收；可控事件流覆盖乱序完成、补充、刷新与独立停止，不调用模型。
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));
const frontend = resolve(root, "frontend"), harness = resolve(frontend, ".cache/parallel-chat");
await mkdir(harness, { recursive: true });
await writeFile(resolve(harness, "index.html"), '<!doctype html><html lang="zh-CN" data-theme="light"><meta charset="UTF-8"><div id="root"></div><script type="module" src="./main.tsx"></script></html>');
await writeFile(resolve(harness, "main.tsx"), `import {useState} from 'react';import {createRoot} from 'react-dom/client';import {ChatView} from '../../src/chat/ChatView';import '../../src/shared/styles.css';import '../../src/shared/hermes.css';function Harness(){const [value,setValue]=useState('');return <main className="app-shell" style={{display:'block'}}><ChatView value={value} onChange={setValue} conversationId="conversation" onConversation={()=>{}} onRun={()=>{}} account={{id:'user',name:'测试'}} identity={{user:{id:'user',name:'测试',email:'test@example.org',platform_admin:false},csrf_token:'test-csrf'}} /></main>;}createRoot(document.getElementById('root')!).render(<Harness/>);`);
const server = await createServer({root:frontend,server:{host:"127.0.0.1",port:15175,strictPort:true}});
let browser;
const wait = () => { let release; const promise = new Promise(r=>release=r); return {promise,release}; };
try {
  await server.listen();
  browser = await chromium.launch({channel:process.platform === "win32" ? "msedge" : undefined, headless:true});
  const page = await browser.newPage({viewport:{width:1200,height:1000}});
  const errors=[]; page.on("pageerror",e=>errors.push(e.message));
  const rows=[], tasks=[], gates={}, requests=[], eventRequests={};
  let sequence=0;
  const files=[{filename:"研究报告.pdf",size:1024,available:true,download_url:"/api/v1/research/11111111-1111-4111-8111-111111111111/files/pdf"}];
  function task(id,seq,prompt){return {id,message_sequence:seq,prompt_preview:prompt,status:"running",error_code:null,provider:"mock",model:"mock-v1",charged_tokens:0,input_tokens:null,output_tokens:null,events_url:`/api/v1/runs/${id}/events`};}
  await page.route("**/api/v1/**",async route=>{
    const request=route.request(), path=new URL(request.url()).pathname.replace("/api/v1",""); let json;
    if(path==="/me/agents")json={items:[{id:"agent",version_id:"version",name:"通用助手",number:1,config:{provider:"mock"}}],next_cursor:null};
    else if(path==="/me/skills")json={items:[],next_cursor:null};
    else if(path==="/conversations/conversation/messages")json={items:rows,next_cursor:null};
    else if(path==="/conversations/conversation/runs")json={items:[...tasks].reverse()};
    else if(path==="/runs"&&request.method()==="POST"){
      const body=request.postDataJSON();requests.push(body);assert.ok(request.headers()["idempotency-key"]);
        const id=["a","b","c"][tasks.length];
        rows.push({id:`user-${++sequence}`,sequence,role:"user",content:body.message,run_id:id});
        json=task(id,sequence,body.message);tasks.push(json);gates[id]=wait();
        if(id==="c")await new Promise(r=>setTimeout(r,400));
    }else if(path.endsWith("/events")){
      const id=path.split("/")[2], t=tasks.find(t=>t.id===id);
      eventRequests[id]=(eventRequests[id]||0)+1;
      if(id!=="b"&&["running","paused"].includes(t.status))await gates[id].promise;
      if(t.status!=="cancelled"){
        t.status="succeeded";
        if(!rows.some(m=>m.id===id))rows.push({id,sequence:++sequence,role:"assistant",run_id:id,content:id==="a"?"研究文件已生成。":"RAG 是检索增强生成。",attachments:id==="a"?files:[]});
      }
      const payload=`id: ${id}:1\ndata: ${JSON.stringify({sequence:1,type:"text_delta",payload:{text:id==="b"?"RAG 是检索增强生成。":"研究文件已生成。"}})}\n\nid: ${id}:2\ndata: ${JSON.stringify({sequence:2,type:t.status==="cancelled"?"error":"completed",payload:t.status==="cancelled"?{error_code:"cancelled"}:{}})}\n\n`;
      await route.fulfill({contentType:"text/event-stream",body:payload}).catch(()=>{});return;
    }else if(path.endsWith("/pause")||path.endsWith("/resume")){
      const id=path.split("/")[2];json=tasks.find(t=>t.id===id);
      json.status=path.endsWith("/pause")?"paused":"running";
      json.event_sequence=(json.event_sequence||0)+1;
    }else if(path.endsWith("/cancel")){
      const id=path.split("/")[2];json=tasks.find(t=>t.id===id);json.status="cancelled";gates[id].release();
    }else if(/^\/runs\/[abc]$/.test(path))json=tasks.find(t=>t.id===path.split("/")[2]);
    else if(path.startsWith("/research/runs/"))json={status:"running",items:[]};
    else throw new Error(`Unexpected ${request.method()} ${path}`);
    await route.fulfill({json});
  });
  await page.goto("http://127.0.0.1:15175/.cache/parallel-chat/index.html");
  const input=page.getByLabel("任务输入",{exact:true}), send=page.getByRole("button",{name:"发送任务",exact:true});
  await input.fill("研究甲并生成PDF");await send.click();
  await page.locator('[data-run-id="a"] .task-thinking').waitFor();
  const composer=page.locator(".composer-controls");
  await composer.getByRole("button",{name:"暂停思考",exact:true}).click();
  await composer.getByRole("button",{name:"继续思考",exact:true}).waitFor();
  assert.equal(tasks[0].status,"paused");
  assert.match(await page.locator('[data-run-id="a"] .task-thinking').innerText(),/已暂停思考/);
  assert.equal(eventRequests.a,1);
  await page.screenshot({path:resolve(harness,"parallel-paused.png"),fullPage:true});
  await page.setViewportSize({width:400,height:850});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  const pauseBox=await composer.getByRole("button",{name:"继续思考",exact:true}).boundingBox();
  const stopBox=await composer.getByRole("button",{name:"停止思考",exact:true}).boundingBox();
  assert.ok(pauseBox.x>=0&&stopBox.x+stopBox.width<=400&&stopBox.y+stopBox.height<=850);
  await page.screenshot({path:resolve(harness,"parallel-paused-mobile.png"),fullPage:true});
  await page.setViewportSize({width:1200,height:1000});
  await page.reload();
  await composer.getByRole("button",{name:"继续思考",exact:true}).waitFor();
  assert.equal(tasks[0].status,"paused");
  await composer.getByRole("button",{name:"继续思考",exact:true}).click();
  await composer.getByRole("button",{name:"暂停思考",exact:true}).waitFor();
  assert.equal(tasks[0].status,"running");
  assert.equal(eventRequests.a,2);
  await input.fill("顺便解释一下RAG");await input.press("Enter");
  await page.locator('[data-run-id="b"]').getByText("RAG 是检索增强生成。",{exact:true}).waitFor();
  assert.equal(await page.locator('[data-run-id="a"] .task-thinking').count(),1);
  assert.equal(await page.getByRole("button",{name:"补充要求",exact:true}).count(),0);
  await page.reload();await page.locator('[data-run-id="a"] .task-thinking').waitFor();
  await page.locator('[data-run-id="b"]').getByText("RAG 是检索增强生成。",{exact:true}).waitFor();
  await page.screenshot({path:resolve(harness,"parallel-running.png"),fullPage:true});
  gates.a.release();
  await page.locator('[data-run-id="a"]').getByRole("link",{name:"下载 研究报告.pdf",exact:true}).waitFor().catch(async error=>{
    console.log('DEBUG',JSON.stringify({tasks,rows,errors,text:await page.locator('body').innerText()}));
    await page.screenshot({path:resolve(harness,"failure.png"),fullPage:true});throw error;
  });
  assert.equal(await page.locator('[data-run-id="b"] .chat-attachments').count(),0);
  assert.equal(await page.locator('[data-run-id="a"] .message.assistant').count(),1);
  await input.fill("另一个独立任务");await send.click();await input.fill("发送期间继续输入的草稿");
  await page.locator('[data-run-id="c"] .task-thinking').waitFor();
  assert.equal(await input.inputValue(),"发送期间继续输入的草稿");
  assert.equal(await composer.getByRole("button",{name:"停止思考",exact:true}).isEnabled(),true);
  await composer.getByRole("button",{name:"停止思考",exact:true}).click();
  await page.locator('[data-run-id="c"] .task-thinking').waitFor({state:"detached"});
  assert.equal(tasks[0].status,"succeeded");assert.equal(tasks[2].status,"cancelled");
  assert.equal(await input.inputValue(),"发送期间继续输入的草稿");
  await send.waitFor();
  await page.setViewportSize({width:400,height:850});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:resolve(harness,"parallel-mobile.png"),fullPage:true});
  await page.evaluate(()=>document.documentElement.dataset.theme="dark");
  await page.screenshot({path:resolve(harness,"parallel-dark.png"),fullPage:true});
  assert.deepEqual(errors,[]);
  console.log("PASS: background chat, pause/resume, paused refresh, unchanged SSE connection, stop button, reply ownership, attachments, draft preservation, mobile/dark UI");
}finally{await browser?.close();await server.close();}
