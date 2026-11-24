import {plot} from '../components/common/chart.js';
import {feedback, field} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {REVIEW, STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

const SAMPLE_STATUS = {satisfied:['已满足','complete'], drawn:['已补抽','complete'], blocked:['暂不补抽','blocked']};

function samplingPanel(job, refresh) {
  const config = workspace.spec.sampling;
  const day = job.params?.posting_date;
  if (!config || !day) return null;
  const message = feedback();
  const output = el('div', {class:'sampling-output'});
  const preview = button('预览缺额', {class:'quiet','data-testid':'sample-preview'});
  const draw = button('补抽', {class:'primary','data-testid':'sample-draw'});
  const form = el('form', {class:'fields', onSubmit:event=>event.preventDefault()},
    el('div', {class:'form-grid'}, config.fields.map(item=>field(item))),
    el('div', {class:'actions'}, preview, session.canWrite && draw, message));
  const request = () => ({dataset_id:job.dataset_id,
    budget:Number(new FormData(form).get('budget')),
    zone_minimum:Number(new FormData(form).get('zone_minimum')),
    group_minimum:Number(new FormData(form).get('group_minimum'))});
  const fill = state => {
    for (const item of config.fields) {
      const input = form.elements.namedItem(item.key);
      if (input && state[item.key] !== undefined) input.value = state[item.key];
    }
  };
  const renderResult = (result, saved) => {
    const [label,state] = SAMPLE_STATUS[result.status] || ['—','pending'];
    output.replaceChildren(
      el('div',{class:'actions'},pill(saved ? `${label} · 已落账` : `${label} · 仅预览`,state),
        p(result.message,'subtle')),
      metrics([
        {label:'当日预算（张）', value:result.budget, detail:`已占 ${result.occupied} · 剩余 ${result.remaining}`},
        {label:'本次补抽（张）', value:result.drawn.length, detail:result.status === 'blocked' ? '暂不补抽' : '按最少券补足'},
        {label:'缺口（张）', value:result.shortfall, detail:result.unfillable.length ? `凑不齐：${result.unfillable.join('、')}` : '各校区 / 档口组最低数'},
      ]),
      table([{key:'transaction_key',label:'抽中券（原核销流水）'}, {key:'zone',label:'校区'},
        {key:'merchant_group',label:'档口组'}, {key:'source',label:'来源'},
        {key:'discrepancies',label:'差异笔数'}, {key:'note',label:'说明'}], result.vouchers,
        {caption:'当日已抽中的券', empty:'当日还没有抽中的券。'}),
      table([{key:'scope',label:'范围'}, {key:'name',label:'名称'}, {key:'minimum',label:'最低数'},
        {key:'covered',label:'已覆盖'}, {key:'shortfall',label:'缺额'}, {key:'state',label:'状态'}],
        result.targets, {caption:'最低抽查数覆盖与缺额'}));
  };
  const call = (action, task, saved) => handler(action, message, async () => {
    const result = await task(day, request());
    renderResult(result, saved);
    await refresh();
  });
  preview.addEventListener('click', call(preview, workspace.previewSample, false));
  if (session.canWrite) draw.addEventListener('click', call(draw, workspace.drawSample, true));
  workspace.sample(day).then(async state=>{
    if (!output.isConnected) return;
    if (state.updated_at) fill(state);
    if (!state.result && !state.draws.length) return;
    try {
      // 补账重算：已抽券照占名额，按当前批次重新核对覆盖（预览不落账）。
      const result = await workspace.previewSample(day, request());
      if (output.isConnected) renderResult(result, false);
    } catch (error) {
      message.textContent = error.message;
    }
  }).catch(()=>{});
  return panel(config.title, p(config.note, 'subtle'), form, output);
}

export async function results(route, refresh) {
  const {spec,runs,datasets} = workspace;
  const id = route.id || runs[0]?.id;
  if (!id) return panel(spec.pages[3],empty('还没有可查看的运行结果。','#run','前往运行'));
  const job = await workspace.getRun(id);
  const source = datasets.find(item => item.id === job.dataset_id);
  const message = feedback();
  const finished = item => item.status === 'complete' || item.status === 'failed';
  if (!finished(job)) {
    const original = location.hash;
    const status = el('p',{role:'status'},'后台正在计算，完成后会自动展示。');
    poll(()=>workspace.getRun(id),finished,()=>location.hash===original)
      .then(result=>{if (result && status.isConnected && location.hash===original) refresh();
        else if(status.isConnected) status.textContent='仍在执行，请点击刷新检查。';})
      .catch(error=>{if(status.isConnected) status.textContent=error.message;});
    return panel(job.name,pill(STATUS[job.status],job.status),status,button('刷新',{class:'quiet',onClick:refresh}));
  }
  const download = button('下载结果 CSV',{class:'quiet','data-testid':'export',onClick:event=>
    handler(download,message,()=>workspace.download(`/runs/${job.id}/export`,`${spec.short_name}-${job.id}.csv`))(event)});
  const history = el('label',{class:'field run-picker',for:'run-picker'},el('span',{},'切换运行记录'),
    el('select',{id:'run-picker',onChange:event=>{location.hash=`#results/${event.target.value}`;}},
      (runs.some(item=>item.id===job.id) ? runs : [job,...runs]).map(item=>el('option',{value:item.id,selected:item.id===job.id},item.name))));
  const result = job.result;
  const sampling = result ? samplingPanel(job, refresh) : null;
  const analysis = result ? el('section',{class:'analysis','data-testid':'analysis-complete'},
    el('div',{class:'result-head'},p(job.name,'eyeline'),h(2,result.headline),p(result.note,'intro-copy'),
      el('div',{class:'actions'},pill(STATUS[job.status],job.status),download,message)),
    metrics(result.metrics),
    result.charts.map(plot),
    (result.sections || []).map(item=>panel(item.title,p(item.text))),
    sampling,
    result.tables.map(item=>panel(item.title,table(item.columns,item.rows,{caption:item.title}))))
    : panel(job.name,pill(STATUS[job.status],job.status),p(job.error,'feedback'),link('返回调整参数',`#run/${job.dataset_id}`,{class:'quiet'}));
  const inputs = spec.params.map(param=>el('div',{},el('dt',{},param.label),el('dd',{},value(job.params[param.key]))));
  return el('div',{},el('div',{class:'two-col'},el('div',{class:'wide-panel'},analysis),
    el('aside',{class:'sidebar-panel'},panel('运行来源',history,
      source && link(source.name,`#inputs/${source.id}`),p(date(job.finished_at),'subtle'),
      el('dl',{class:'dictionary'},inputs)),
    result && panel(spec.decision, pill(REVIEW[job.review],job.review),
      link('打开复核记录',`#review/${job.id}`,{class:'quiet'})))));
}
