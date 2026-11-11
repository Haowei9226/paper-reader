import type {Block,Selection,TextAlias} from './types';

export function locateQuote(text:string,quote:string,near:number):{start:number;end:number}|null{
  const starts:number[]=[],ends:number[]=[];let normalized='',offset=0;
  for(const char of text){const value=/\s/.test(char)?' ':char.normalize('NFKC');if(value===' '&&normalized.endsWith(' ')){offset+=char.length;continue}normalized+=value;for(let j=0;j<value.length;j++){starts.push(offset);ends.push(offset+char.length)}offset+=char.length;}
  const needle=quote.normalize('NFKC').replace(/-\s+(?=[a-z])/g,'').replace(/\s+/g,' ').trim();if(!needle)return null;
  const found:{start:number;end:number}[]=[];for(let position=normalized.indexOf(needle);position>=0;position=normalized.indexOf(needle,position+1))found.push({start:starts[position],end:ends[position+needle.length-1]});
  if(!found.length)return null;found.sort((a,b)=>Math.abs(a.start-near)-Math.abs(b.start-near));return found[0];
}

export function textSelection(root:HTMLElement,blocks:Block[]):Selection|null {
  const selection=window.getSelection();
  if(!selection||selection.isCollapsed||!selection.rangeCount||!selection.toString().trim())return null;
  const range=selection.getRangeAt(0);
  if(!root.contains(range.startContainer)||!root.contains(range.endContainer))return null;
  const selectedPages=root.querySelectorAll<HTMLElement>('[data-reading-page]');
  const textBlocks=selectedPages.length
    ? [...selectedPages].filter(el=>range.intersectsNode(el)).flatMap(el=>[...el.querySelectorAll<HTMLElement>('[data-text-block]')])
    : [...root.querySelectorAll<HTMLElement>('[data-text-block]')];
  const elements=textBlocks.filter(el=>range.intersectsNode(el)&&el.getClientRects().length);
  const byId = new Map(blocks.map(block => [block.id, block]));
  const targets=[];const translated_targets=[];
  const visibleText:{source:string[];translation:string[]}={source:[],translation:[]};
  let side:'source'|'translation'='source';
  for(const el of elements){
    const block=byId.get(el.dataset.blockId!);if(!block)continue;
    const translated=el.dataset.side==='translation';side=translated?'translation':'source';
    const text=translated?block.translation?.text:block.text;if(!text)continue;
    const before=range.cloneRange();before.selectNodeContents(el);
    const offset=Number(el.dataset.textStart??0), limit=Number(el.dataset.textEnd??text.length);
    let start=offset,end=limit;
    if(el.contains(range.startContainer)){before.setEnd(range.startContainer,range.startOffset);start=offset+before.toString().length;}
    if(el.contains(range.endContainer)){before.selectNodeContents(el);before.setEnd(range.endContainer,range.endOffset);end=offset+before.toString().length;}
    start=Math.min(start,limit);end=Math.min(end,limit);if(end<=start)continue;
    const target={block_id:block.id,start,end,quote:text.slice(start,end),page:block.page};
    visibleText[side].push(target.quote);
    if(translated&&block.translation){
      translated_targets.push({...target,translation_id:block.translation.id});
      if(el.dataset.textAliases) for(const alias of JSON.parse(el.dataset.textAliases) as TextAlias[]){
        translated_targets.push({...target,block_id:alias.block_id,translation_id:alias.translation_id,
          start:alias.start+start-offset,end:alias.start+end-offset});
      }
    }else targets.push(target);
  }
  if(!targets.length&&!translated_targets.length)return null;
  const rect=range.getBoundingClientRect();
  return {side,targets,translated_targets,text:visibleText[side].join('\n'),x:Math.min(window.innerWidth-260,Math.max(12,rect.left+rect.width/2-100)),y:rect.bottom+8,top:rect.top};
}

export function relativeRange(el:HTMLElement):{start:number;end:number}|null{
  const s=window.getSelection();if(!s?.rangeCount||s.isCollapsed)return null;
  const r=s.getRangeAt(0);if(!el.contains(r.startContainer)||!el.contains(r.endContainer))return null;
  const before=r.cloneRange();before.selectNodeContents(el);before.setEnd(r.startContainer,r.startOffset);const start=before.toString().length;
  before.setEnd(r.endContainer,r.endOffset);const end=before.toString().length;
  return {start,end};
}
