import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../src/citations.ts',import.meta.url),'utf8');
const compiled = ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}}).outputText;
const {citationRanges,citationLabel} = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
const references = [
  {number:-1,display_label:'Smith, 2020',citation_keys:[{author:'smith',year:'2020'}]},
  {number:-2,display_label:'Jones et al., 2021',citation_keys:[{author:'jones',year:'2021'}]},
  {number:-3,citation_keys:[{author:'smith',year:'2021'}]},
  {number:-4,citation_keys:[{author:'smith',year:'2020a'}]},
  {number:-5,citation_keys:[{author:'smith',year:'2020b'}]},
  {number:-6,citation_keys:[{author:'smithjones',year:'2022'}]},
  {number:-7,citation_keys:[{author:'张三',year:'2020'}]},
  {number:-8,citation_keys:[{author:'worldhealthorganization',year:'n.d.'}]},
  {number:-9,citation_keys:[{author:'vanderwaals',year:'2019'}]},
  {number:-20,citation_keys:[{author:'世界卫生组织',year:'无日期'}]},
  {number:-21,citation_keys:[{author:'𠮷田',year:'2018'}]},
];
const links = {known:new Set([1,2,3,4,5,6,7,8]),entries:references,open(){}};
function block(text,overrides={}) {
  return {text,kind:'paragraph',reading_role:'text',notation:[],geometry:{boxes:[],lines:[]},...overrides};
}
function matches(text,options=links,overrides={}) {
  return citationRanges(block(text,overrides),text,options).map(range => ({
    text:text.slice(range.start,range.end),numbers:range.numbers,
  }));
}

test('numbered brackets, parentheses, ranges and adjoining groups use real reference numbers',() => {
  for (const text of ['[1, 3-4; 7–8]','［１，３－４；７～８］','【1、3–4;7-8】','(1, 3–4; 7-8)','（1，3-4；7–8）']) {
    assert.deepEqual(matches('Prior work ' + text),[{text,numbers:[1,3,4,7,8]}]);
  }
  assert.deepEqual(matches('Prior work [1][2] [3–4].'),[{text:'[1][2] [3–4]',numbers:[1,2,3,4]}]);
  assert.deepEqual(matches('Prior work [1]–[3].'),[{text:'[1]–[3]',numbers:[1,2,3]}]);
  assert.deepEqual(matches('Prior work ［１］－［３］.'),[{text:'［１］－［３］',numbers:[1,2,3]}]);
  assert.deepEqual(matches('Prior work [1]–[3].',{...links,known:new Set([1,3])}),[]);
  assert.deepEqual(matches('Prior work [3]–[1].'),[]);
  for (const marker of ['[2, pp. 12–14]','[2,p.12]','[2, pages 12-14]','［２，页１２－１４］']) {
    assert.deepEqual(matches('Prior work ' + marker),[{text:marker,numbers:[2]}]);
  }
  assert.deepEqual(matches('Prior work [1,99] and [5-3].'),[]);
  assert.deepEqual(matches('Prior work [1]',new Set([1])),[{text:'[1]',numbers:[1]}]);
  for (const word of ['reported','studies']) assert.deepEqual(matches(word + '(1)'),[{text:'(1)',numbers:[1]}]);
});

test('equations, figures, coordinates, list labels and standalone years stay ordinary text',() => {
  for (const text of ['Eq. (1)','Equation(1)','Figure (2)','Table (1)','Section (3)','n = (2)',
    'point (1,2)','(1) The first option','A value (2) participants','x(1)','sin(1)','log(2)','In (2020)']) {
    assert.deepEqual(matches(text,{...links,known:new Set([...links.known,2020])}),[],text);
  }
});

test('author-year parentheses and narratives resolve only exact, unique bibliography keys',() => {
  const examples = [
    ['Prior work (Smith,2020; Jones et al.,2021).','(Smith,2020; Jones et al.,2021)',[-1,-2]],
    ['Smith (2020) reported results.','Smith (2020)',[-1]],
    ['Smith et al. (2020) reported results.','Smith et al. (2020)',[-1]],
    ['Smith and Jones (2022) reported results.','Smith and Jones (2022)',[-6]],
    ['(Smith & Jones, 2022, pp. 117–18)','(Smith & Jones, 2022, pp. 117–18)',[-6]],
    ['(Smith,2020,2021)','(Smith,2020,2021)',[-1,-3]],
    ['(Smith,2020a,2020b)','(Smith,2020a,2020b)',[-4,-5]],
    ['(Smith,2020a/b)','(Smith,2020a/b)',[-4,-5]],
    ['(Smith,2020a,b)','(Smith,2020a,b)',[-4,-5]],
    ['As shown [Smith 2020].','[Smith 2020]',[-1]],
    ['【张三，2020】','【张三，2020】',[-7]],
    ['根据张三等（2020）的研究','张三等（2020）',[-7]],
    ['World Health Organization (n.d.) recommends this.','World Health Organization (n.d.)',[-8]],
    ['van der Waals (2019) reported this.','van der Waals (2019)',[-9]],
    ['世界卫生组织（无日期）建议采用此方法。','世界卫生组织（无日期）',[-20]],
    ['根据𠮷田（2018）的研究','𠮷田（2018）',[-21]],
  ];
  for (const [text,quote,numbers] of examples) assert.deepEqual(matches(text),[{text:quote,numbers}],text);
  assert.deepEqual(matches('Smith (2020) and Jones (2021).'),[
    {text:'Smith (2020)',numbers:[-1]},{text:'Jones (2021)',numbers:[-2]},
  ]);
  assert.deepEqual(matches('(Smith,2020)',{...links,known:new Set()}),[{text:'(Smith,2020)',numbers:[-1]}]);
  assert.deepEqual(matches('(Smith,2020)',{...links,entries:[...references,{number:-10,citation_keys:[{author:'smith',year:'2020'}]}]}),[]);
  assert.deepEqual(matches('Blacksmith (2020); Smith and Brown (2022); unknown (2021).'),[]);
  assert.deepEqual(matches('(Smith,2020; Unknown,2021)'),[]);
  assert.equal(citationLabel([-1,-2],links),'查看参考文献 Smith, 2020 Jones et al., 2021');
  assert.equal(citationLabel([-100],links),'查看参考文献 未编号条目');
});

test('tables allow strong citations while headings, formulas and reference entries stay unlinked',() => {
  assert.deepEqual(matches('Value [1] (Smith,2020) (2)',links,{reading_role:'table'}),[
    {text:'[1] (Smith,2020)',numbers:[1,-1]},
  ]);
  for (const role of ['title','heading','formula','reference','extra']) {
    assert.deepEqual(matches('Prior work [1] (Smith,2020)',links,{reading_role:role}),[]);
  }
});

test('superscript citations require genuine superscript evidence and meaningful prose',() => {
  assert.deepEqual(matches('Prior studies1 support this.',links,{notation:[[13,14,'sup']]}),[{text:'1',numbers:[1]}]);
  assert.deepEqual(matches('Prior studies¹,² support this.'),[{text:'¹,²',numbers:[1,2]}]);
  for (const text of ['x²','m²','10²','A footnote¹','Prior studies use square meters²',
    'Prior studies use 10⁻³ values','Prior studies use e⁺³ values']) assert.deepEqual(matches(text),[],text);
  assert.deepEqual(matches('Prior studies1 support this.'),[]);
  const text = 'Prior studies12 support this.';
  const boxes = [...text].map((_,index) => [index * 5,20,index * 5 + 5,30]);
  boxes[13] = [65,16,68,22];
  boxes[14] = [68,16,71,22];
  assert.deepEqual(matches(text,{...links,known:new Set([12])},{geometry:{boxes,lines:Array(text.length).fill(0)}}),[{text:'12',numbers:[12]}]);
  const sourceBlock = block('Prior studies1 support this.',{notation:[[13,14,'sup']]});
  assert.deepEqual(citationRanges(sourceBlock,'译文需要1个结果',links),[]);
});

test('all source ranges retain UTF-16 offsets after emoji and mixed scripts',() => {
  const text = '🧪 根据张三等（2020）研究 [1][2]。';
  const found = citationRanges(block(text),text,links);
  assert.deepEqual(found.map(range => text.slice(range.start,range.end)),['张三等（2020）','[1][2]']);
  assert.ok(found.every((range,index) => !index || found[index - 1].end <= range.start));
});
