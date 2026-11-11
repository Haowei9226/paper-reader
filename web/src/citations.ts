import type {Block} from './types';

export type CitationReference = {
  number:number;
  numbered?:boolean;
  display_label?:string;
  citation_keys?:readonly {author:string;year:string}[];
};
export type CitationLinks = {
  known:ReadonlySet<number>;
  open:(numbers:number[])=>void;
  entries?:readonly CitationReference[];
};
export type CitationRange = {start:number;end:number;numbers:number[]};

const digit = '[0-9０-９]{1,4}';
const dash = '[-‐‑–—−－~～]';
const separator = '[,，、;；]';
const item = `${digit}(?:\\s*${dash}\\s*${digit})?`;
const group = `\\s*(${item}(?:\\s*${separator}\\s*${item})*)\\s*`;
const locator = `(?:\\s*[,，]\\s*(?:pp?\\.?|pages?\\.?|页)\\s*[0-9０-９]+(?:\\s*[-–—－,，、]\\s*[0-9０-９]+)*)?`;
const numericBrackets = new RegExp(`\\[${group}${locator}\\]|【${group}${locator}】|［${group}${locator}］|\\(${group}${locator}\\)|（${group}${locator}）`, 'giu');
const singleBracket = `(?:\\[\\s*${digit}\\s*\\]|［\\s*${digit}\\s*］|【\\s*${digit}\\s*】)`;
const bracketRange = new RegExp(`(${singleBracket})\\s*${dash}\\s*(${singleBracket})`, 'gu');
const allBrackets = /\(([^()\[\]【】［］（）]{1,600})\)|（([^()\[\]【】［］（）]{1,600})）|\[([^()\[\]【】［］（）]{1,600})\]|【([^()\[\]【】［］（）]{1,600})】|［([^()\[\]【】［］（）]{1,600})］/gu;
const splitGroup = new RegExp(separator, 'u');
const splitRange = new RegExp(dash, 'u');
const excluded = new Set(['reference', 'extra', 'image', 'formula', 'embedded', 'title', 'heading', 'subheading']);
const years = /(?<![\p{L}\p{N}])([12１２][0-9０-９]{3}[a-z]?|n\.?\s*d\.?|无日期)(?![\p{L}\p{N}])/giu;
const mathematicalContext = /(?:\b(?:eq(?:uation)?s?|fig(?:ure)?s?|tables?|sections?|sec|appendi(?:x|ces)|algorithms?|theorems?|lemmas?|propositions?|definitions?|conditions?)\.?|公式|式|图|表|章节|第)\s*$/iu;
const measuredContext = /(?:\b(?:n|N|p|df|age|ages|sample|size|score|rating|scale|level|version|points?|coordinates?|position|vector|range)|样本|人数|年龄|评分|量表|等级|版本|坐标|向量)\s*(?:[=:：＝]\s*)?$/iu;
const citationContext = /\b(?:stud(?:y|ies)|research|works?|reports?|reported|reviews?|literature|according|cited|references?|see|shown|showed|findings?|results?|proposed)\b|研究|文献|参见|报道|表明|指出|提出|认为|结果/iu;

type AuthorIndex = Map<string,Map<string,Set<number>>>;

function identity(text:string) {
  return text.normalize('NFKC').toLowerCase().replace(/ß/g,'ss').replace(/ς/g,'σ').replace(/[^\p{L}\p{N}]/gu,'');
}

function yearKey(text:string) {
  const value = text.normalize('NFKC').toLowerCase().replace(/\s+/g,'');
  if (/^n\.?d\.?$/.test(value) || value === '无日期') return 'n.d.';
  return /^(?:18|19|20|21)\d{2}[a-z]?$/.test(value) ? value : '';
}

function authorIndex(entries:readonly CitationReference[]) {
  const index:AuthorIndex = new Map();
  for (const entry of entries) for (const key of entry.citation_keys ?? []) {
    const author = identity(key.author), year = yearKey(key.year);
    if (!author || !year) continue;
    let dates = index.get(author);
    if (!dates) {dates = new Map(); index.set(author,dates)}
    let matches = dates.get(year);
    if (!matches) {matches = new Set(); dates.set(year,matches)}
    matches.add(entry.number);
  }
  return index;
}

function numericValues(text:string, known:ReadonlySet<number>) {
  const values = new Set<number>();
  for (const part of text.normalize('NFKC').split(splitGroup)) {
    const [first,last = first] = part.split(splitRange).map(value => Number(value.trim()));
    if (!Number.isInteger(first) || !Number.isInteger(last) || first <= 0 || last < first || last - first > 200) return null;
    for (let value = first; value <= last; value++) {
      if (!known.has(value)) return null;
      values.add(value);
    }
  }
  return values.size ? [...values] : null;
}

function authorVariants(text:string) {
  const author = text.trim().replace(/^(?:see(?:\s+also)?|cf\.?|e\.g\.?|例如|参见)[,，\s]+/iu,'')
    .replace(/[,，\s]+$/u,'').replace(/(?:\s+et\s+al\.?|等)\s*$/iu,'');
  return [...new Set([
    identity(author),
    identity(author.replace(/\band\b|[&和与及、]/giu,'')),
  ])].filter(Boolean);
}

function resolveAuthors(authors:readonly string[], dates:readonly string[], index:AuthorIndex) {
  const numbers = new Set<number>();
  for (const date of dates) {
    const matches = new Set<number>();
    for (const author of authors) for (const number of index.get(author)?.get(date) ?? []) matches.add(number);
    if (matches.size !== 1) return null;
    numbers.add([...matches][0]);
  }
  return [...numbers];
}

function authorYears(text:string) {
  const matches = [...text.matchAll(years)].filter(match => yearKey(match[0]));
  if (!matches.length) return null;
  const dates:string[] = [];
  let previousEnd = matches[0].index!;
  for (const match of matches) {
    const between = text.slice(previousEnd,match.index);
    if (dates.length && !/^[\s,，、/&及和–—-]*$/u.test(between)) break;
    const year = yearKey(match[0]);
    dates.push(year);
    previousEnd = match.index! + match[0].length;
    if (/\d{4}[a-z]$/.test(year)) {
      let tail = text.slice(previousEnd);
      for (;;) {
        const suffix = /^\s*[,，/、]\s*([a-z])(?=$|[\s,，;/、])/iu.exec(tail);
        if (!suffix) break;
        dates.push(year.slice(0,4) + suffix[1].toLowerCase());
        previousEnd += suffix[0].length;
        tail = tail.slice(suffix[0].length);
      }
    }
  }
  return {author:text.slice(0,matches[0].index).replace(/[,，\s]+$/u,''),dates};
}

function normalizedSuffix(text:string, stripConnectors:boolean) {
  const omitted = stripConnectors ? [...text.matchAll(/\band\b|[和与及、]/giu)] : [];
  let value = '', offset = 0;
  const positions:number[] = [];
  for (const char of text) {
    if (!omitted.some(match => match.index! <= offset && offset < match.index! + match[0].length)) {
      for (const letter of identity(char)) {
        value += letter;
        for (let unit = 0; unit < letter.length; unit++) positions.push(offset);
      }
    }
    offset += char.length;
  }
  return {value,positions};
}

function narrativeAuthor(text:string, end:number, dates:readonly string[], index:AuthorIndex) {
  const prefix = text.slice(Math.max(0,end - 200),end).replace(/\s+$/u,'');
  const authorEnd = prefix.replace(/(?:\s+et\s+al\.?|等)\s*$/iu,'');
  const candidates:{start:number;author:string;numbers:number[]}[] = [];
  for (const strip of [false,true]) {
    const normalized = normalizedSuffix(authorEnd,strip);
    for (const author of index.keys()) {
      if (!normalized.value.endsWith(author)) continue;
      const localStart = normalized.positions[normalized.value.length - author.length];
      if (localStart === undefined) continue;
      const before = authorEnd.slice(0,localStart);
      if (/(?:\band|&|和|与|及|、)\s*$/iu.test(before) &&
          !/[)）\]】］]\s*(?:and|&|和|与|及|、)\s*$/iu.test(before)) continue;
      const previous = authorEnd[localStart - 1] ?? '', first = authorEnd[localStart];
      if (/\p{Script=Latin}/u.test(previous) && /\p{Script=Latin}/u.test(first)) continue;
      const numbers = resolveAuthors([author],dates,index);
      if (numbers) candidates.push({start:Math.max(0,end - 200) + localStart,author,numbers});
    }
  }
  candidates.sort((a,b) => a.start - b.start);
  const first = candidates[0];
  if (!first) return null;
  const sameStart = candidates.filter(candidate => candidate.start === first.start);
  if (sameStart.some(candidate => candidate.numbers.join(',') !== first.numbers.join(','))) return null;
  return first;
}

function authorYearRanges(text:string, index:AuthorIndex) {
  const ranges:CitationRange[] = [];
  if (!index.size) return ranges;
  for (const match of text.matchAll(allBrackets)) {
    const content = match.slice(1).find(value => value !== undefined)!;
    const parts = content.split(/[;；]/u);
    let authors:string[] = [], start = match.index!, valid = true;
    const numbers = new Set<number>();
    for (const part of parts) {
      const citation = authorYears(part);
      if (!citation) {valid = false; break}
      if (citation.author) authors = authorVariants(citation.author);
      if (!authors.length) {
        const narrative = narrativeAuthor(text,match.index!,citation.dates,index);
        if (!narrative) {valid = false; break}
        start = narrative.start;
        authors = [narrative.author];
      }
      const found = resolveAuthors(authors,citation.dates,index);
      if (!found) {valid = false; break}
      found.forEach(number => numbers.add(number));
    }
    if (valid && numbers.size) ranges.push({start,end:match.index! + match[0].length,numbers:[...numbers]});
  }
  return ranges;
}

function parentheticalNumber(text:string, start:number, end:number) {
  const before = text.slice(Math.max(0,start - 60),start);
  const after = text.slice(end);
  if (!before.trim() || mathematicalContext.test(before) || measuredContext.test(before)) return false;
  const word = /[\p{Script=Latin}]+$/u.exec(before)?.[0];
  if (/[\p{Script=Latin}\d=+*/^_]$/u.test(before) &&
      !(word && citationContext.test(word)) && !/\b(?:see|cf|refs?)$/iu.test(before)) return false;
  if (/^\s*(?:%|percent|participants?|people|years?|months?|cm|mm|kg|人|年|岁|分|点|毫[米升])/iu.test(after)) return false;
  return true;
}

function superscriptRanges(block:Block, text:string, known:ReadonlySet<number>) {
  if (text !== block.text || (block.reading_role ?? block.kind) === 'table') return [];
  const spans = (block.notation ?? []).filter(([, ,style]) => style === 'sup').map(([start,end]) => ({start,end}));
  for (const match of text.matchAll(/[⁰¹²³⁴⁵⁶⁷⁸⁹]+(?:\s*[,，;；–—-]\s*[⁰¹²³⁴⁵⁶⁷⁸⁹]+)*/gu)) {
    spans.push({start:match.index!,end:match.index! + match[0].length});
  }
  const boxes = block.geometry?.boxes ?? [], rows = block.geometry?.lines ?? [];
  if (boxes.length === text.length) for (const match of text.matchAll(/[0-9]+(?:\s*[,，;；–—-]\s*[0-9]+)*/gu)) {
    const start = match.index!, end = start + match[0].length;
    const selected = boxes.slice(start,end).filter((box):box is number[] => !!box);
    const peers = boxes.filter((box,index) => box && rows[index] === rows[start] && /[\p{L}]/u.test(text[index]) &&
      Math.abs(index - start) < 100) as number[][];
    if (!selected.length || peers.length < 6) continue;
    const normalHeight = peers.map(box => box[3] - box[1]).sort((a,b) => a - b)[Math.floor(peers.length / 2)];
    const baseline = peers.map(box => box[3]).sort((a,b) => a - b)[Math.floor(peers.length / 2)];
    if (selected.every(box => box[3] - box[1] <= normalHeight * .85 && box[3] < baseline - normalHeight * .15)) {
      spans.push({start,end});
    }
  }
  const ranges:CitationRange[] = [];
  for (const span of spans) {
    const value = text.slice(span.start,span.end), numbers = numericValues(value,known);
    if (!numbers) continue;
    const before = text.slice(Math.max(0,span.start - 100),span.start);
    if (/[\d=+\-−⁻⁺*/^_]\s*$/u.test(before) ||
        /(?:^|[^\p{L}])(?:[a-z]|cm|mm|km|kg|mol|ft|ha|met(?:er|re)s?|seconds?|inches|feet)\s*$/iu.test(before)) continue;
    if (numbers.length === 1 && !citationContext.test(before)) continue;
    if (/\b(?:footnotes?|notes?)\s*$/iu.test(before)) continue;
    ranges.push({...span,numbers});
  }
  return ranges;
}

export function citationRanges(block:Block, text:string, source:ReadonlySet<number>|CitationLinks,
                               excludedRanges:readonly {start:number;end:number}[] = []):CitationRange[] {
  if (excluded.has(block.reading_role ?? block.kind)) return [];
  const links = 'known' in source ? source : {known:source,entries:[]};
  const numeric = links.known;
  const ranges = authorYearRanges(text,authorIndex(links.entries ?? []));
  const intervals:{start:number;end:number}[] = [];
  for (const match of text.matchAll(bracketRange)) {
    const start = match.index!, end = start + match[0].length;
    intervals.push({start,end});
    const first = match[1].normalize('NFKC').replace(/\D/g,'');
    const last = match[2].normalize('NFKC').replace(/\D/g,'');
    const numbers = numericValues(first + '-' + last,numeric);
    if (numbers && !ranges.some(range => range.start < end && start < range.end)) ranges.push({start,end,numbers});
  }
  for (const match of text.matchAll(numericBrackets)) {
    const start = match.index!, end = start + match[0].length;
    if (intervals.some(range => range.start < end && start < range.end)) continue;
    if (ranges.some(range => range.start < end && start < range.end)) continue;
    const parentheses = match[0].startsWith('(') || match[0].startsWith('（');
    if (parentheses && ((block.reading_role ?? block.kind) === 'table' || !parentheticalNumber(text,start,end))) continue;
    const content = match.slice(1).find(value => value !== undefined)!;
    const numbers = numericValues(content,numeric);
    if (numbers && !(parentheses && numbers.every(number => number >= 1800 && number <= 2199))) {
      ranges.push({start,end,numbers});
    }
  }
  ranges.push(...superscriptRanges(block,text,numeric));
  const visible = ranges.filter(range => !excludedRanges.some(note => range.start < note.end && note.start < range.end));
  visible.sort((a,b) => a.start - b.start || b.end - a.end);
  const result:CitationRange[] = [];
  for (const range of visible) {
    const previous = result.at(-1);
    if (previous && range.start < previous.end) continue;
    if (previous && /^\s*$/u.test(text.slice(previous.end,range.start))) {
      previous.end = range.end;
      previous.numbers = [...new Set([...previous.numbers,...range.numbers])];
    } else result.push({...range});
  }
  return result;
}

export function citationLabel(numbers:number[], links?:Pick<CitationLinks,'entries'>) {
  const entries = new Map(links?.entries?.map(entry => [entry.number,entry]));
  const labels = numbers.map(number => {
    const entry = entries.get(number);
    if (entry?.display_label) return entry.display_label;
    return number < 0 || entry?.numbered === false ? '未编号条目' : `[${number}]`;
  });
  return '查看参考文献 ' + labels.join(' ');
}
