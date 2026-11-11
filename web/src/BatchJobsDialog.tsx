import {useState} from 'react';
import {Globe2,Languages,LoaderCircle} from 'lucide-react';
import {post} from './api';
import {ErrorLine,Modal} from './components';
import {PROVIDERS,PROVIDER_NAMES,type Bootstrap,type Job,type Paper,type Provider} from './types';

export type BatchResult = {
  queued:Job[];
  updated:number;
  skipped:{paper_id:string;title:string;reason:string}[];
};
export type BatchAction = 'translate'|'metadata';

export default function BatchJobsDialog({action,papers,bootstrap,onClose,onQueued,onJobs}:{
  action:BatchAction;
  papers:Paper[];
  bootstrap:Bootstrap;
  onClose:()=>void;
  onQueued:(result:BatchResult)=>void;
  onJobs:()=>void;
}) {
  const configured = {local:true,deepseek:bootstrap.deepseek_configured,openai:bootstrap.openai_configured};
  const [provider,setProvider] = useState<Provider>(()=>configured[bootstrap.settings.provider]?bootstrap.settings.provider:'local');
  const [force,setForce] = useState(false);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [result,setResult] = useState<BatchResult>();
  const translating = action==='translate';
  const title = translating?'批量翻译':'批量联网补全';
  const ready = papers.some(paper=>paper.has_attachment&&(translating ? paper.parse_state==='ready'
    : !['pending','processing'].includes(paper.parse_state)));
  const Icon = translating?Languages:Globe2;

  async function submit() {
    setBusy(true);
    setError('');
    try {
      const response = await post<BatchResult>('/papers/batch',{
        action,paper_ids:papers.map(paper=>paper.id),...(translating?{provider,force}:{}),
      });
      setResult(response);
      onQueued(response);
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <Modal title={title} onClose={()=>{if(!busy)onClose();}} footer={result?<><button type="button" className="button" onClick={onClose}>关闭</button>
    {result.queued.length>0&&<button type="button" className="button primary" onClick={onJobs}>查看任务与进度</button>}</>
    :<><button type="button" className="button" disabled={busy} onClick={onClose}>取消</button>
      <button type="button" className="button primary" disabled={busy||!ready||(translating&&!configured[provider])} onClick={()=>void submit()}>
        {busy?<LoaderCircle size={15} className="spin"/>:<Icon size={15}/>}
        {translating?'加入翻译队列':'加入补全队列'}
      </button></>}>
    {result?<><p className="batch-summary" role="status">已加入 {result.queued.length} 篇
      {result.skipped.length>0&&`，${result.skipped.length} 篇未加入`}</p>
      {result.skipped.length>0&&<ul className="batch-paper-list">
        {result.skipped.map(item=><li key={item.paper_id}><strong>{item.title}</strong><span>{item.reason}</span></li>)}
      </ul>}
    </>:<>
      <p className="batch-summary">已选 {papers.length} 篇文献，{translating?'按篇排队翻译全文':'按篇补全缺失的出版信息'}</p>
      {!translating&&<p className="small muted">使用 DOI 或标题查询 Crossref 和 DataCite，补齐出版日期、出版社和条目类型。已有值和人工设置会保留。</p>}
      <ul className="batch-paper-list" aria-label="所选文献">
        {papers.map(paper=>{
          let detail = '';
          if (!paper.has_attachment) detail = '找不到原 PDF';
          else if (translating) detail = paper.parse_state!=='ready'?'正文尚未就绪':paper.language==='zh'?'中文 → 英文':'英文 → 中文';
          else if (['pending','processing'].includes(paper.parse_state)) detail = '等待 PDF 转换完成';
          return <li key={paper.id}><strong title={paper.title}>{paper.title}</strong>{detail&&<span>{detail}</span>}</li>;
        })}
      </ul>
      {translating&&<><label className="field">翻译服务<select value={provider} disabled={busy} onChange={e=>setProvider(e.target.value as Provider)}>
        {PROVIDERS.map(p=><option key={p} value={p} disabled={!configured[p]}>{PROVIDER_NAMES[p]}{configured[p]?'':'（未配置）'}</option>)}
      </select></label>
      <label className="check-row"><input type="checkbox" checked={force} disabled={busy}
        onChange={e=>setForce(e.target.checked)}/>重新翻译已有的机器译文</label>
      <p className="small muted">{force?'将保留人工修改的译文。':'只补译缺失或需要修复的内容，保留人工修改。'}
        {provider==='local'?'文献内容在本机处理。':`所选文献正文和各篇相关术语会发送至 ${PROVIDER_NAMES[provider]}。`}
      </p></>}
    </>}
    <ErrorLine text={error}/>
  </Modal>;
}
