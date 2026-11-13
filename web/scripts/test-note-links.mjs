import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

async function module(path) {
  const source = await readFile(new URL(path,import.meta.url),'utf8');
  const compiled = ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}}).outputText;
  return import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
}
const {noteRanges,noteLabel} = await module('../src/noteLinks.ts');
const {documentIndexRevision,noteBlockIds} = await module('../src/documentNotes.ts');
const {citationRanges} = await module('../src/citations.ts');
const entries = [
  {id:'note:footnote:p1:0',kind:'footnote',label:'1'},
  {id:'note:footnote:p2:0',kind:'footnote',label:'1'},
  {id:'note:endnote:p9:0',kind:'endnote',label:'2'},
  ...['*','†','‡','§'].map(label => ({id:'note:footnote:symbol:' + label,kind:'footnote',label})),
];
function block(text,id='source',page=1,translation=null) {
  return {id,page,text,kind:'paragraph',reading_role:'text',geometry:{boxes:[],lines:[]},translation};
}
function marker(block,quote,noteId,evidence='superscript',start=block.text.indexOf(quote)) {
  return {note_id:noteId,evidence,anchor:{block_id:block.id,page:block.page,start,end:start + quote.length,quote}};
}
function links(markers) {return {entries,markers,open(){}}}

test('source index revision ignores translation and assistant progress',()=>{
  const source={id:'source',kind:'parse',state:'done',progress:2,updated:10};
  const translate={id:'translation',kind:'translate',state:'running',progress:1,updated:10};
  const revision=documentIndexRevision([source,translate]);
  assert.equal(documentIndexRevision([{...translate,progress:40,updated:20},source]),revision);
  assert.equal(documentIndexRevision([source,{...translate,state:'done'},
    {id:'assistant',kind:'reading',state:'done'},{id:'alignment',kind:'align',state:'running'},
    {id:'backup',kind:'backup',state:'done'}]),revision);
  assert.equal(documentIndexRevision([]),documentIndexRevision([translate]));
});

test('source index revision tracks source job lifecycle but remains order and progress stable',()=>{
  const jobs=[{id:'parse-2',kind:'parse',state:'running',progress:1,updated:10},
    {id:'ocr-1',kind:'ocr',state:'running',progress:2,updated:10},
    {id:'metadata-3',kind:'metadata',state:'running',progress:0,updated:10}];
  const revision=documentIndexRevision(jobs);
  assert.equal(documentIndexRevision([...jobs].reverse().map(job=>({...job,progress:99,updated:500}))),revision);
  for(let i=0;i<jobs.length;i++) {
    assert.notEqual(documentIndexRevision(jobs.map((job,index)=>index===i?{...job,state:'done'}:job)),revision);
  }
  assert.notEqual(documentIndexRevision([...jobs,{id:'new-paper',kind:'parse',state:'queued'}]),revision);
});

test('only fully covered source-note blocks leave the body flow',()=>{
  const full=block('1 A note.\nIts continuation.','full'), mixed=block('Body text. 1 A note.','mixed');
  const fragment=(source,start=0,end=source.text.length)=>({block_id:source.id,page:source.page,start,end,quote:source.text.slice(start,end)});
  const notes=[{fragments:[fragment(full,0,9),fragment(full,10),fragment(mixed,11)]}];
  assert.deepEqual([...noteBlockIds([full,mixed],notes)],['full']);
  assert.equal(noteBlockIds([full],[{fragments:[{...fragment(full),page:2}]}]).size,0);
  assert.equal(noteBlockIds([full],[{fragments:[{...fragment(full),quote:'changed'}]}]).size,0);
});

test('page-numbered labels use independent note IDs and source anchors',() => {
  const first = block('An observation1.','page-one',1), second = block('An observation1.','page-two',2);
  const index = links([marker(first,'1',entries[0].id),marker(second,'1',entries[1].id)]);
  assert.deepEqual(noteRanges(first,first.text,index).map(range => range.noteIds),[[entries[0].id]]);
  assert.deepEqual(noteRanges(second,second.text,index).map(range => range.noteIds),[[entries[1].id]]);
  assert.equal(noteLabel([entries[0].id],index),'查看脚注 1');
  assert.equal(noteLabel(['missing'],index),'查看注释');
});

test('symbols and repeated endnote citations consume only resolved markers',() => {
  const source = block('Alpha* beta† gamma‡ delta§. Note (2) is reused (2).');
  const markers = ['*','†','‡','§'].map(symbol => marker(source,symbol,'note:footnote:symbol:' + symbol));
  markers.push(marker(source,'(2)',entries[2].id,'pdf_link'));
  markers.push(marker(source,'(2)',entries[2].id,'pdf_link',source.text.lastIndexOf('(2)')));
  const ranges = noteRanges(source,source.text,links(markers));
  assert.deepEqual(ranges.map(range => source.text.slice(range.start,range.end)),['*','†','‡','§','(2)','(2)']);
  assert.deepEqual(ranges.slice(-2).map(range => range.noteIds),[[entries[2].id],[entries[2].id]]);
  assert.deepEqual(noteRanges(source,source.text,links([])),[]);
  assert.equal(noteLabel([entries[2].id],links(markers)),'查看尾注 2');
});

test('unknown, stale, non-proven and conflicting note targets remain plain text',() => {
  const source = block('Text1 and (2).');
  const valid = marker(source,'1',entries[0].id);
  const stale = {...valid,anchor:{...valid.anchor,quote:'2'}};
  const missing = {...valid,note_id:'missing'};
  const wrongPage = {...valid,anchor:{...valid.anchor,page:3}};
  const unproven = {...valid,evidence:'number'};
  for (const candidate of [stale,missing,wrongPage,unproven]) assert.deepEqual(noteRanges(source,source.text,links([candidate])),[]);
  assert.deepEqual(noteRanges(source,source.text,links([valid,{...valid,note_id:entries[1].id}])),[]);
  assert.deepEqual(noteRanges(source,source.text,links([valid,valid])).map(range => range.noteIds),[[entries[0].id]]);
  assert.deepEqual(noteRanges(source,'Different text1',links([valid])),[]);
});

test('translation links require exact alignment or an unchanged aligned fragment',() => {
  const sourceText = 'A finding1 follows.';
  const start = sourceText.indexOf('1');
  const translated = '发现1仍然成立。';
  const source = block(sourceText,'source',1,{text:translated,segments:[
    {source_start:start,source_end:start + 1,target_start:2,target_end:3},
  ]});
  const index = links([marker(source,'1',entries[0].id)]);
  assert.deepEqual(noteRanges(source,translated,index),[{start:2,end:3,noteIds:[entries[0].id]}]);
  source.translation.segments = [{source_start:0,source_end:sourceText.length,target_start:0,target_end:translated.length}];
  assert.deepEqual(noteRanges(source,translated,index),[]);
  source.translation.text = '译文 finding1 继续';
  source.translation.segments = [{source_start:2,source_end:10,target_start:3,target_end:11}];
  assert.deepEqual(noteRanges(source,source.translation.text,index),[{start:10,end:11,noteIds:[entries[0].id]}]);
  source.translation.text += '1';
  source.translation.segments.push({source_start:start,source_end:start + 1,
    target_start:source.translation.text.length - 1,target_end:source.translation.text.length});
  assert.deepEqual(noteRanges(source,source.translation.text,index),[]);
});

test('UTF-16 boundaries are checked before using source offsets',() => {
  const source = block('🧪 Fact1.');
  const index = links([marker(source,'1',entries[0].id)]);
  const ranges = noteRanges(source,source.text,index);
  assert.equal(source.text.slice(ranges[0].start,ranges[0].end),'1');
  const broken = marker(source,source.text.slice(1,2),entries[0].id,'pdf_link',1);
  assert.deepEqual(noteRanges(source,source.text,links([broken])),[]);
});

test('confirmed note markers take precedence over bibliography citations',() => {
  const source = block('Prior research1 is discussed in [1].');
  const start = source.text.indexOf('1');
  source.notation = [[start,start + 1,'sup']];
  const index = links([marker(source,'1',entries[0].id)]);
  const notes = noteRanges(source,source.text,index);
  const citations = citationRanges(source,source.text,{known:new Set([1]),open(){}},notes);
  assert.deepEqual(citations.map(range => source.text.slice(range.start,range.end)),['[1]']);
  assert.deepEqual(notes.map(range => source.text.slice(range.start,range.end)),['1']);
});
