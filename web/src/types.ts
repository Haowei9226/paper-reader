export const PROVIDERS = ['local', 'deepseek', 'openai'] as const;
export type Provider = typeof PROVIDERS[number];
export type CloudProvider = Exclude<Provider, 'local'>;
export const PROVIDER_NAMES: Record<Provider,string> = {local:'本地 Qwen',deepseek:'DeepSeek',openai:'OpenAI'};
export type Layout = 'parallel' | 'interleaved' | 'single';
export type ReadingMode = 'pdf' | 'html';
export type ReadingSide = 'source' | 'translation';
export type Locator = {page?:number;block_id?:string;mode?:ReadingMode;side?:ReadingSide;offset?:number;scroll?:number;page_fraction?:number;saved_at?:number};
export type FindHit = {block_id:string;page:number;side:ReadingSide;translation_id:string|null;start:number;end:number;before:string;match:string;after:string};
export type ReferenceAnchor = {block_id:string;page:number;start:number;end:number;quote:string};
export type ReferenceEntry = {
  number:number;
  numbered?:boolean;
  display_label?:string;
  citation_keys?:{author:string;year:string}[];
  text:string;
  doi:string;
  url:string;
  url_kind?:'doi'|'url'|'search';
  anchor:ReferenceAnchor;
  library_match:{paper_id:string;title:string}|null;
  library_match_method?:'doi'|'title_author_year'|null;
};
export function referenceLabel(entry:Pick<ReferenceEntry,'number'|'numbered'|'display_label'>) {
  return entry.display_label || (entry.numbered===false || entry.number<0 ? '未编号条目' : `[${entry.number}]`);
}
export type ReferenceIndex = {entries:ReferenceEntry[];heading:ReferenceAnchor|null};
export type NoteReference = Pick<ReferenceEntry,'text'|'url'|'url_kind'|'library_match'|'library_match_method'>;
export type DocumentNote = {
  id:string;
  kind:'footnote'|'endnote';
  label:string;
  anchor:ReferenceAnchor;
  text:string;
  fragments:ReferenceAnchor[];
  references:NoteReference[];
};
export type DocumentNoteMarker = {note_id:string;anchor:ReferenceAnchor;evidence:'superscript'|'pdf_link'};
export type DocumentNotesIndex = {entries:DocumentNote[];markers:DocumentNoteMarker[]};
export type PublicationStatus = ''|'published'|'unpublished'|'internal';
export type Paper = { id:string; filename:string; original_filename:string; metadata_status:'pending'|'queued'|'local'|'matched'|'review'|'offline'; metadata_source:string; title:string; authors:string; year:string; doi:string; abstract:string; source:string; item_type:string; publication_status:PublicationStatus; publication_date:string; publisher:string; last_read_at:number|null; has_attachment:boolean; language:string; status:'unread'|'reading'|'read'; starred:number; pages:number; parse_state:string; parse_error:string; parse_engine:string; tags:string[]; collections:string[]; annotations_count:number; translated_count:number; blocks_count:number; outline:{level:number;title:string;page:number}[]; locator:Locator; deleted_at:number|null; created:number; updated:number; ocr_pending_pages?:number; ocr_empty_pages?:number; ocr_pages?:number };
export type Collection = {id:string;name:string;parent_id:string|null;count:number};
export type CloudStatus = {deepseek_configured:boolean;openai_configured:boolean};
export type Settings = {provider:Provider;local_url:string;local_model:string;deepseek_model:string;openai_model:string;context_tokens:number;font_size:number;layout:Layout;parse_engine:string;metadata_lookup:boolean;auto_rename:boolean};
export type Bootstrap = CloudStatus & {counts:Record<string,number>;collections:Collection[];tags:{name:string;count:number}[];settings:Settings;version:string};
export type Segment = {source_start:number;source_end:number;target_start:number;target_end:number;notation?:boolean;
  source_block_id?:string;target_block_id?:string;sentence_join?:string;notation_styles?:Notation[]};
export type Translation = {id:string;block_id:string;paper_id:string;text:string;segments:Segment[];provider:string;model:string;manual:number;active:number;created:number};
export type ReadingRole = 'title' | 'heading' | 'subheading' | 'caption' | 'reference' | 'extra' | 'text' | 'image' | 'table' | 'formula' | 'embedded';
export type TextBounds = [number,number];
export type TextAlias = {block_id:string;translation_id:string;start:number;end:number};
export type Notation = [number,number,'math'|'sub'|'sup'];
export type Block = {id:string;paper_id:string;page:number;position:number;kind:string;reading_role?:ReadingRole;reading_element?:string;reading_title?:string;reading_breaks?:number[];reading_heading?:TextBounds;continues_previous?:boolean;text:string;bbox:number[];geometry:{boxes:(number[]|null)[];lines:number[]};asset:string|null;section:string;translation:Translation|null;notation?:Notation[];equation?:{asset:string;number:string|null;block_ids:string[];width:number;height:number}};
export type ReadingTable = {id:string;asset:string;caption_id:string|null;block_ids:string[];position:number;rows:number;columns:number;header_rows:number;widths:number[];
  cells:{row:number;column:number;rowspan:number;colspan:number;joined:boolean;parts:{block_id:string;start:number;end:number;target:TextBounds|null}[]}[]};
export type PageContinuationLink = {block_id:string;page:number;current_block_id?:string;join_word:boolean};
export type PageReadingContinuation = {previous?:PageContinuationLink;next?:PageContinuationLink};
export type PageData = {paper_id:string;number:number;width:number;height:number;rotation:number;blocks:Block[];tables?:ReadingTable[];ocr?:boolean;reading_continuation?:PageReadingContinuation;reading_source?:string};
export type Target = {block_id:string;start:number;end:number;quote?:string;page?:number;rects?:number[][]};
export type TranslatedTarget = Target & {translation_id:string};
export type Annotation = {id:string;paper_id:string;targets:Target[];translated_targets:TranslatedTarget[];quote:string;note:string;color:string;alignment:string;created:number;updated:number};
export type Selection = {side:'source'|'translation';targets:Target[];translated_targets:TranslatedTarget[];text:string;x:number;y:number;top:number};
export type CounterpartRange = Target & {page:number;quote:string;quality:'manual'|'mapped'|'paragraph';translation_id?:string};
export type Counterpart = {side:ReadingSide;ranges:CounterpartRange[];missing:number};
export type Job = {id:string;paper_id:string|null;paper_title?:string;kind:string;state:string;payload:Record<string,unknown>;progress:number;total:number;message:string;result:Record<string,unknown>;created:number;updated:number};
export type AIResult = {id:string;paper_id:string;kind:string;question:string;answer:string;citations:{block_id:string;quote:string;page:number}[];provider:string;model:string;scope:string;created:number};
export const COLOR_LABELS:Record<string,string> = {yellow:'黄色',green:'绿色',blue:'蓝色',pink:'粉色'};
export const STATUS_LABELS:Record<string,string> = {unread:'待读',reading:'在读',read:'已读'};
export const PUBLICATION_STATUS_LABELS:Record<PublicationStatus,string> = {
  '':'未标注', published:'已发表', unpublished:'未发表手稿', internal:'内部资料',
};
export const ITEM_TYPE_LABELS:Record<string,string> = {
  'journal-article':'期刊论文', 'conference-paper':'会议论文', book:'图书', 'book-chapter':'图书章节',
  preprint:'预印本', 'posted-content':'在线稿件', thesis:'学位论文', report:'报告', dataset:'数据集', other:'其他',
};
