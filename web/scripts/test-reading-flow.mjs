import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';

async function module(path) {
  const source = await readFile(new URL(path,import.meta.url),'utf8');
  const compiled = ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}}).outputText;
  return import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
}
const {readingContinuations} = await module('../src/readingFlow.ts');
const {capturePosition,scrollForPosition} = await module('../src/readerPosition.ts');
const bundled = await build({
  entryPoints:[fileURLToPath(new URL('../src/HtmlView.tsx',import.meta.url))],
  bundle:true,write:false,format:'esm',platform:'node',jsx:'automatic',loader:{'.css':'empty'},
  plugins:[{name:'shared-react',setup(bundler) {
    bundler.onResolve({filter:/^(?:react(?:\/.*)?|lucide-react)$/},args => ({path:import.meta.resolve(args.path),external:true}));
  }}],
});
let HtmlView;
try {
  HtmlView = (await import('data:text/javascript;base64,' + Buffer.from(bundled.outputFiles[0].text).toString('base64'))).default;
} catch (error) {
  throw new Error('HTML rendering test could not load the component. ' + error.message);
}
const block = (id,page,text,continues_previous=false) => ({
  id,page,text,continues_previous,position:Number(id.slice(-1)) || 0,paper_id:'paper',
  kind:'paragraph',reading_role:'text',bbox:[40,100,500,130],geometry:{boxes:[],lines:[]},translation:null,
});

test('paragraph and heading boundaries keep canonical source text in one anchored block',()=>{
  const source=block('source1',2,'I. INTRODUCTION A new paragraph. RQ1. A question?');
  source.reading_heading=[0,15];source.reading_breaks=[16,33];
  const markup=renderToStaticMarkup(createElement(HtmlView,props(2,[source],undefined)));
  assert.equal((markup.match(/data-paragraph-break=""/g)??[]).length,2);
  assert.ok(markup.includes('reading-inline-heading'));
  assert.ok(markup.replace(/<[^>]+>/g,'').includes(source.text));
  assert.equal((markup.match(/data-block-anchor="source1"/g)??[]).length,1);
});

test('a mixed source title uses its confirmed clean display title without rewriting its text',()=>{
  const source=block('title1',1,'A clean title Alice Example Example University');
  source.reading_role='extra';source.reading_element='title';source.reading_title='A clean title';
  const view=props(1,[source],undefined);view.paper.title=source.text;
  const markup=renderToStaticMarkup(createElement(HtmlView,view));
  assert.match(markup,/<h2 class="article-title"[^>]*>A clean title<\/h2>/);
  assert.ok(markup.includes(source.text));
});

test('article title and authors on the second physical page remain visible',()=>{
  const title=block('title1',2,'A journal article title');title.reading_role='title';title.reading_element='title';title.reading_title=title.text;
  const author=block('author1',2,'Alice Example');author.reading_role='extra';author.reading_element='author';
  const markup=renderToStaticMarkup(createElement(HtmlView,props(2,[title,author],undefined)));
  assert.ok(markup.includes('article-cover'));
  assert.ok(markup.includes('article-authors'));
  assert.ok(markup.includes('data-reading-element="title"'));
});

test('known author names stay separate from affiliations merged in a PDF credit block',()=>{
  const credit=block('author1',1,'Alice Example Example University');credit.reading_role='extra';credit.reading_element='author';
  const view=props(1,[credit],undefined);view.paper.authors='Alice Example';
  const markup=renderToStaticMarkup(createElement(HtmlView,view));
  assert.match(markup,/<p class="article-authors">Alice Example<\/p>/);
  assert.ok(markup.includes('Alice Example Example University'));
});

test('retained source anchors stay accessible outside the new body flow',()=>{
  const retained=block('legacy1',2,'An old running header');retained.kind='legacy';retained.reading_role='extra';retained.reading_element='legacy_source';
  const body=block('body1',2,'Fresh body text belongs to the new extraction.');
  const markup=renderToStaticMarkup(createElement(HtmlView,props(2,[retained,body],undefined)));
  assert.ok(markup.includes('保留的原文定位'));
  assert.equal((markup.match(/data-block-anchor="legacy1"/g)??[]).length,1);
  assert.ok(markup.indexOf(body.text)<markup.indexOf(retained.text));
});
const slot = (number,blocks) => ({number,data:{blocks}});
function connect(left,right,joinWord=false) {
  const source = left.data.blocks.at(-1), target = right.data.blocks[0];
  left.data.reading_continuation = {...left.data.reading_continuation,
    next:{block_id:target.id,page:right.number,current_block_id:source.id,join_word:joinWord}};
  right.data.reading_continuation = {...right.data.reading_continuation,
    previous:{block_id:source.id,page:left.number,current_block_id:target.id,join_word:joinWord}};
}
function props(page,blocks,continuation,side='source') {
  return {paper:{id:'paper',title:'',authors:'',language:'en',year:'',source:'',pages:3},
    page,blocks,continuation,side,annotations:new Map(),onSelect(){},onAnnotation(){},onTranslate(){},fontSize:17,continuous:true};
}

test('a confirmed boundary moves the complete first paragraph and preserves source data',() => {
  const left = slot(1,[block('left1',1,'First fragment'),block('left2',1,'continues',true)]);
  const right = slot(2,[block('right1',2,'on this page',true),block('right2',2,'with more text.',true),block('right3',2,'A new paragraph.')]);
  connect(left,right);
  const before = JSON.stringify([left,right]);
  const plans = readingContinuations([left,right]);
  assert.deepEqual(plans.get(1).extensions.get('left2').map(block => block.id),['right1','right2']);
  assert.deepEqual([...plans.get(2).hidden],['right1','right2']);
  assert.equal(JSON.stringify([left,right]),before);
});

test('unloaded, unconfirmed, stale or nonadjacent pages keep their own paragraphs',() => {
  const left = slot(1,[block('left1',1,'A fragment')]), right = slot(2,[block('right1',2,'continues.')]);
  connect(left,right);
  assert.equal(readingContinuations([left,{number:2}]).get(1).extensions.size,0);
  const oneSided = structuredClone(right);
  delete oneSided.data.reading_continuation;
  assert.equal(readingContinuations([left,oneSided]).get(1).extensions.size,0);
  const stale = structuredClone(right);
  stale.data.reading_continuation.previous.current_block_id = 'missing';
  assert.equal(readingContinuations([left,stale]).get(1).extensions.size,0);
  assert.equal(readingContinuations([left,{...right,number:3}]).get(1).extensions.size,0);
});

test('whole-page paragraph chains move once and retain confirmed word boundaries',() => {
  const first = slot(1,[block('first1',1,'semi-')]);
  const second = slot(2,[block('second1',2,'structured'),block('second2',2,'program-',true)]);
  const third = slot(3,[block('third1',3,'ming examples.')]);
  connect(first,second,true);
  connect(second,third,true);
  const plans = readingContinuations([first,second,third]);
  assert.deepEqual(plans.get(1).extensions.get('first1').map(block => block.id),['second1','second2','third1']);
  assert.deepEqual([...plans.get(1).joinWords],['first1','second2']);
  assert.equal(plans.get(2).extensions.size,0);
  assert.deepEqual([...plans.get(2).hidden],['second1','second2']);
  assert.deepEqual([...plans.get(3).hidden],['third1']);
  assert.equal(first.data.blocks[0].text,'semi-');
});

test('whole-block notes stay outside continuation paragraphs',() => {
  const left = slot(1,[block('left1',1,'The')]);
  const right = slot(2,[block('note1',2,'1 This is a footnote.'),block('right1',2,'system continues.')]);
  left.data.reading_continuation = {next:{block_id:'right1',page:2,current_block_id:'left1',join_word:false}};
  right.data.reading_continuation = {previous:{block_id:'left1',page:1,current_block_id:'right1',join_word:false}};
  assert.equal(readingContinuations([left,right]).get(1).extensions.size,0);
  const plans = readingContinuations([left,right],new Set(['note1']));
  assert.deepEqual(plans.get(1).extensions.get('left1').map(block => block.id),['right1']);
  assert.ok(!plans.get(2).hidden.has('note1'));
});

test('normal HtmlView rendering joins across the boundary once with actual page anchors',() => {
  const left = slot(1,[block('left1',1,'We studied')]), right = slot(2,[block('right1',2,'collective decisions.')]);
  connect(left,right);
  const plans = readingContinuations([left,right]);
  const first = renderToStaticMarkup(createElement(HtmlView,props(1,left.data.blocks,plans.get(1))));
  const second = renderToStaticMarkup(createElement(HtmlView,props(2,right.data.blocks,plans.get(2))));
  assert.match(first,/reading-joined/);
  assert.match(first,/data-page-start="2"/);
  assert.match(first,/data-block-anchor="right1" data-block-page="2"/);
  assert.equal((first + second).match(/data-block-anchor="right1"/g).length,1);
  assert.ok(!second.includes('这一页暂无可重排的正文'));
  assert.ok(first.includes('We studied') && first.includes('collective decisions.'));
});

test('confirmed same-paragraph translations remain joined after sentence punctuation',() => {
  const blocks = [block('left1',1,'First source.'),block('left2',1,'Second source.',true)];
  blocks.forEach((block,index) => {block.translation = {
    id:'translation-' + index,text:index ? 'Second translated.' : 'First translated.',segments:[],manual:0,
  }});
  const markup = renderToStaticMarkup(createElement(HtmlView,props(1,blocks,undefined,'translation')));
  assert.equal(markup.match(/class="reading-block reading-joined"/g).length,1);
  assert.ok(markup.includes('First translated.') && markup.includes('Second translated.'));
});

function element(dataset,top,height) {
  return {dataset,getBoundingClientRect:() => ({top,bottom:top + height,height}),
    querySelector:() => null,querySelectorAll:() => [],closest:() => null};
}
function scene() {
  const left = element({blockAnchor:'left',blockPage:'1',readingSide:'source'},-20,40);
  const right = element({blockAnchor:'right',blockPage:'2',readingSide:'source'},20,80);
  const second = element({readingPage:'2'},700,200), first = element({readingPage:'1'},-100,800);
  first.querySelectorAll = () => [left,right];
  const boundary = element({pageStart:'2',readingSide:'source'},20,80);
  const pages = {children:[first,second],
    querySelector:selector => selector.includes('"2"') ? second : first,
    querySelectorAll:() => [boundary]};
  const pane = {scrollTop:100,scrollHeight:1500,clientHeight:500,getBoundingClientRect:() => ({top:0}),
    querySelector:selector => selector === '[data-reading-pages]' ? pages : null,
    querySelectorAll:selector => selector.includes('"right"') ? [right] : selector.includes('"left"') ? [left] : []};
  return {pane,right,left,second,pages};
}
globalThis.CSS ??= {escape:value => value};

test('capture and restore locate moved fragments by their actual page and global block ID',() => {
  const {pane} = scene();
  const position = capturePosition(pane,1,'source',null);
  assert.equal(position.page,2);
  assert.equal(position.block_id,'right');
  assert.equal(scrollForPosition(pane,{page:2,block_id:'right',offset:.25},'source',null),108);
  assert.equal(scrollForPosition(pane,{page:2},'source',null),88);
});

test('an empty cross-page translation slot restores to the owning translated sentence',() => {
  const {pane} = scene();
  const slot = element({blockAnchor:'right',blockPage:'2',readingSide:'translation',
    readingOwner:'left',ownerStart:'20',ownerEnd:'40'},700,0);
  const owner = element({blockAnchor:'left',blockPage:'1',readingSide:'translation'},150,100);
  owner.querySelector = () => ({textContent:'x'.repeat(100)});
  pane.querySelectorAll = selector => selector.includes('"right"') ? [slot] : selector.includes('"left"') ? [owner] : [];
  assert.equal(scrollForPosition(pane,{page:2,block_id:'right',offset:.5},'translation',null),248);
});

for (const side of ['source','translation']) test(`capture ignores positive rectangles inside closed details on the ${side} side`,()=>{
  const {pane,pages}=scene();
  const hidden=element({blockAnchor:'hidden-author',readingSide:side},-112,400);
  const visible=element({blockAnchor:'visible-body',readingSide:side},-112,900);
  const details={querySelector:()=>({contains:()=>false}),closest:()=>details};
  hidden.parentElement=details;
  pages.children[0].querySelectorAll=()=>[hidden,visible];
  const position=capturePosition(pane,1,side,null);
  assert.equal(position.block_id,'visible-body');
  assert.equal(position.side,side);
  assert.equal(position.offset,.16);
  // Native visibility also protects browsers where hidden content is laid out.
  hidden.parentElement=undefined;
  hidden.checkVisibility=()=>false;
  assert.equal(capturePosition(pane,1,side,null).block_id,'visible-body');
});

test('a visible summary remains eligible while a summary inside an outer closed fold does not',()=>{
  const {pane,pages}=scene();
  const summary=element({blockAnchor:'summary',readingSide:'source'},20,40);
  const body=element({blockAnchor:'body',readingSide:'source'},20,100);
  const inner={querySelector:()=>({contains:element=>element===summary}),closest:()=>inner};
  summary.parentElement=inner;
  pages.children[0].querySelectorAll=()=>[summary,body];
  assert.equal(capturePosition(pane,1,'source',null).block_id,'summary');
  const outer={querySelector:()=>({contains:()=>false}),closest:()=>outer};
  inner.parentElement=outer;
  assert.equal(capturePosition(pane,1,'source',null).block_id,'body');
});

test('explicit navigation still opens a preserved source target inside closed details',()=>{
  const {pane}=scene();
  const target=element({blockAnchor:'legacy',readingSide:'source'},150,100);
  const details={open:false};
  target.closest=()=>details;
  pane.querySelectorAll=()=>[target];
  assert.equal(scrollForPosition(pane,{page:1,block_id:'legacy',offset:.25},'source',null,{position:{page:1}}),243);
  assert.equal(details.open,true);
});
