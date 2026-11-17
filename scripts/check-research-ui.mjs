// 普通对话文件交付的浏览器验收；接口全部模拟，不消费模型服务。
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";
const root = fileURLToPath(new URL("../", import.meta.url));
const frontend = resolve(root, "frontend");
const harness = resolve(frontend, ".cache/research-ui");
await mkdir(harness, { recursive: true });
await writeFile(resolve(harness,"index.html"), '<!doctype html><html lang="zh-CN" data-theme="light"><meta charset="UTF-8"><title>对话文件验收</title><div id="root"></div><script type="module" src="./main.tsx"></script></html>');
await writeFile(resolve(harness,"main.tsx"), `import {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {ChatView} from '../../src/chat/ChatView';
import '../../src/shared/styles.css'; import '../../src/shared/hermes.css';
function Harness(){ const [value,setValue]=useState('');return <main className="app-shell" style={{display:'block'}}><ChatView value={value} onChange={setValue} conversationId="conversation" onConversation={()=>{}} onRun={()=>{}} account={{id:'user',name:'测试'}} identity={{user:{id:'user',name:'测试',email:'test@example.org',platform_admin:false},csrf_token:'test-csrf'}} /></main>;}
createRoot(document.getElementById('root')!).render(<Harness/>);`);
const server = await createServer({root:frontend,configFile:false,server:{host:"127.0.0.1",port:15174,strictPort:true}});
let browser;
try {
  await server.listen();
  browser=await chromium.launch({channel:process.platform==="win32"?"msedge":undefined,headless:true});
  const page=await browser.newPage({viewport:{width:1200,height:1000}});
  const errors=[]; page.on("pageerror",error=>errors.push(error.message));
  let submitted=false,finished=false;
  const project="11111111-1111-4111-8111-111111111111";
  const pdfName="ACG近期热门话题报告（近3个月）.pdf";
  const files=["docx","pdf","md"].map(format=>({filename:format==="pdf"?pdfName:`研究报告.${format}`,size:format==="pdf"?77824:2048,available:true,download_url:`/api/v1/research/${project}/files/${format}`}));
  const run=()=>({id:"run",status:finished?"succeeded":"running",provider:"mock",model:"mock-v1",charged_tokens:0,input_tokens:null,output_tokens:null,events_url:"/api/v1/runs/run/events"});
  await page.route("**/api/v1/**",async route=>{
    const request=route.request(),path=new URL(request.url()).pathname.replace("/api/v1","");let json;
    if(path==="/me/agents")json={items:[{id:"agent",version_id:"version",name:"研究助手",number:1,config:{provider:"mock"}}],next_cursor:null};
    else if(path==="/me/skills")json={items:[],next_cursor:null};
    else if(path==="/conversations/conversation/messages")json={items:submitted?[{id:"user-message",role:"user",content:"请研究部署限制，生成 Word、PDF 和 Markdown 文件。"},...(finished?[{id:"run",role:"assistant",content:"文件已生成，请下载。",attachments:files}]:[])]:[],next_cursor:null};
    else if(path==="/conversations/conversation/runs")json={items:submitted?[run()]:[]};
    else if(path==="/runs"&&request.method()==="POST"){
      assert.equal(request.headers()["x-csrf-token"],"test-csrf");assert.ok(request.headers()["idempotency-key"]);
      const body=request.postDataJSON();assert.equal(body.conversation_id,"conversation");assert.ok(!body.research_project_id&&!body.research_mode);
      submitted=true;json=run();
    }else if(path==="/runs/run/events"){
      if(!finished)await new Promise(resolve=>setTimeout(resolve,3500));finished=true;
      await route.fulfill({contentType:"text/event-stream",body:'id: run:1\ndata: {"sequence":1,"type":"completed","payload":{}}\n\n'});return;
    }else if(path==="/runs/run")json=run();
    else if(path==="/research/runs/run/progress")json={status:run().status,items:[{title:"部署研究",current_step:"正在阅读资料并生成文件",plan:{questions:[{title:"部署条件",criterion:"核验原始资料"}]}}]};
    else if(path.startsWith(`/research/${project}/files/`)){
      await route.fulfill({body:"下载接口替身",contentType:"application/octet-stream",headers:{"Content-Disposition":`attachment; filename=report.${path.split("/").at(-1)}`}});return;
    }else throw new Error(`Unexpected ${request.method()} ${path}`);
    await route.fulfill({json});
  });
  await page.goto("http://127.0.0.1:15174/.cache/research-ui/index.html");
  await page.getByLabel("任务输入",{exact:true}).fill("请研究部署限制，生成 Word、PDF 和 Markdown 文件。");
  await page.getByRole("button",{name:"发送任务",exact:true}).click();
  await page.getByText("部署条件：核验原始资料",{exact:true}).waitFor();
  await page.getByRole("link",{name:`下载 ${pdfName}`,exact:true}).waitFor();
  assert.equal(await page.getByLabel("生成的文件").locator("a").count(),3);
  const download=page.waitForEvent("download");await page.getByRole("link",{name:`下载 ${pdfName}`,exact:true}).click();
  assert.ok((await download).suggestedFilename().endsWith(".pdf"));
  await page.reload();await page.getByRole("link",{name:"下载 研究报告.docx",exact:true}).waitFor();
  assert.equal(await page.getByLabel("生成的文件").locator("a").count(),3);
  assert.equal(await page.getByRole("checkbox").count(),0);assert.deepEqual(errors,[]);
  await page.screenshot({path:resolve(harness,"research-ui.png"),fullPage:true});
  await page.setViewportSize({width:400,height:850});
  const card=page.locator(".chat-attachments li").filter({has:page.getByText(pdfName,{exact:true})});
  const box=await card.boundingBox();
  assert.ok(box&&box.x+box.width<=400);
  assert.equal(await page.getByRole("link",{name:`下载 ${pdfName}`,exact:true}).innerText(),"下载");
  await page.screenshot({path:resolve(harness,"research-ui-mobile.png"),fullPage:true});
  await page.evaluate(()=>document.documentElement.dataset.theme="dark");
  await page.screenshot({path:resolve(harness,"research-ui-dark.png"),fullPage:true});
  console.log(JSON.stringify({passed:true,checks:["chat-request","no-mode-or-project-input","progress","three-attachments","download","history-reload"],pageErrors:errors}));
}finally{await browser?.close();await server.close();}
