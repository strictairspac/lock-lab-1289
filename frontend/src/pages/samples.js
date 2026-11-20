import {table} from '../components/common/table.js';
import {badge} from '../components/common/badge.js';
import {esc} from '../utils/html.js';
import {session} from '../hooks/session.js';

const readonly = session.user.role === 'viewer';

function sampleRows(items, selectedId) {
  return items.map(s => {
    const child = s.parent_id ? '<small>分装子样本</small>' : '';
    const stopped = s.accepting === false ? ' <span class="badge failed">停止接单</span>' : '';
    const action = s.parent_id ? '' : `<a class="alloc-link" href="#samples/${s.id}">${selectedId === s.id ? '正在分装' : '分装 →'}</a>`;
    return [
      `<strong>${esc(s.code)}</strong><small>${esc(s.title)}${child ? ' · ' + child : ''}</small>`,
      `<b class="quantity">${s.quantity}</b> 份${stopped}`,
      esc(s.location),
      action,
    ];
  });
}

function portionEditor(allocation, sample) {
  if (!sample) return '<div class="notice">从左侧样本行点击「分装 →」，先填写各份编号和数量，生成分配预览；确认前不会改动原台账。</div>';
  if (allocation.batch) return previewPanel(allocation, sample);
  if (sample.parent_id) return '<div class="notice">子样本已是分装结果，请回到其原样本继续处理。</div>';
  if (sample.accepting === false) return `<div class="notice">${esc(sample.code)} 已停止接单。请在下方预览记录或「流转申请」核对既有的分装去向。</div>`;
  if (readonly) return '<div class="notice">观察员仅可查看分装预览与结果，不能填写或确认。</div>';
  const rows = allocation.draft.map((row, index) => `<div class="portion-row">
      <label>分装编号<input name="portion_code" value="${esc(row.code)}" placeholder="QY-261117-A-01" data-index="${index}" required></label>
      <label>份数<input name="portion_quantity" type="number" min="1" value="${esc(row.quantity)}" data-index="${index}" required></label>
      <button type="button" class="link remove-portion" data-index="${index}" ${allocation.draft.length === 1 ? 'disabled' : ''}>删除</button>
    </div>`).join('');
  return `<section class="form-panel alloc-panel"><span class="eyebrow">ALIQUOT SPLIT</span>
    <h2>分装 ${esc(sample.code)} · 可用 ${sample.quantity} 份</h2>
    <p class="help">每份编号唯一、数量合计不超过可用量。系统会把全部待移交申请整笔安置，并让各接收室需要跑的分装点总数最少。</p>
    <form id="portion-form">
      <div id="portion-rows">${rows}</div>
      <button type="button" id="add-portion">再加一份 +</button>
      <button class="primary" ${allocation.busy ? 'disabled' : ''}>生成分配预览 ↗</button>
    </form>
    <p class="help">预览期间仍可并发登记新申请；确认时新申请也会被安置，已看过的去向保持不变。</p>
  </section>`;
}

function previewPanel(allocation, sample) {
  const batch = allocation.batch;
  if (batch.status === 'confirmed' || allocation.result) return confirmedPanel(batch, sample);
  const assignmentRows = batch.assignments.map(a => [
    `<strong>${esc(a.transfer_code)}</strong><small>接收 ${esc(a.destination)} · 共 ${a.quantity} 份</small>`,
    a.portions.map(p => `${esc(p.code)} <small>${p.quantity} 份</small>`).join(''),
  ]);
  const newRows = (batch.new_pending_transfers || []).map(t => [
    `<strong>${esc(t.transfer_code)}</strong><small>预览期间新登记 · ${esc(t.destination)}</small>`,
    `${t.quantity} 份 · 确认时自动安置`,
  ]);
  const unused = (batch.unused_portions || []).map(p => `${esc(p.code)}（${p.quantity} 份）`).join('、') || '无';
  return `<section class="form-panel alloc-panel"><span class="eyebrow">ALLOCATION PREVIEW · #${batch.batch_id}</span>
    <h2>${esc(sample.code)} 分配预览</h2>
    <p class="help">已看过 ${batch.assignments.length} 条去向，确认时这些分配不变。本次使用 <b>${batch.used_portion_count}</b> 个分装点${batch.optimal === false ? '（搜索达到上限，已是最优可行方案中的较好解）' : '（已证明最少）'}，合计 ${batch.expected_quantity - batch.remainder} 份，原样本余 ${batch.remainder} 份不参与分装。</p>
    ${table(['移交申请','分配到的分装份'], assignmentRows)}
    ${newRows.length ? `<div class="notice">预览期间新登记 ${newRows.length} 条申请，确认时将安置：${newRows.map(r => r[0]).join('，')}</div>` : '<div class="notice">预览期间暂无新登记的申请。页面会定时刷新；确认时若出现新单也会一并安置。</div>'}
    <p class="help">未分配分装份：${unused}</p>
    <div class="alloc-actions">
      <button id="alloc-refresh">刷新新单状态</button>
      <button id="alloc-preview-again" class="link">重新填写分装</button>
      <button id="alloc-confirm" class="primary" ${allocation.busy || readonly ? 'disabled' : ''}>确认分装并生效 ↦</button>
    </div>
    <p class="help">确认一次同时完成：子样本入账、移交申请改挂、原样本停止接单。断线后重试不会重复分装。</p>
  </section>`;
}

function confirmedPanel(batch, sample) {
  const result = batch.result || {};
  const rows = (result.transfers || []).map(t => [
    `<strong>${esc(t.transfer_code)}</strong><small>${t.new_during_preview ? '预览期间新登记' : '预览时已看过'} · ${esc(t.destination)}</small>`,
    (t.portions || []).map(p => `${esc(p.code)} <small>${p.quantity} 份</small>`).join(''),
  ]);
  return `<section class="form-panel alloc-panel"><span class="eyebrow">ALLOCATION CONFIRMED · #${batch.batch_id}</span>
    <h2>${esc(sample.code)} 分装已生效 ${badge('completed')}</h2>
    <p class="help">共 ${result.used_portion_count} 个分装点，移交 ${result.portion_total} 份，原样本余 ${result.remainder} 份。子样本入账、申请改挂、原样本停止接单已同笔生效。</p>
    ${table(['移交申请','改挂到的子样本（分装份）'], rows)}
    <p class="help">请求键：<code>${esc(result.request_key || '')}</code> · 校验和：<code>${esc(result.checksum || '')}</code></p>
    <p class="help">使用同一请求键重试确认会返回这同一结果，不会再次分装。</p>
  </section>`;
}

export function samplesPage(items, allocation = {sampleId: null, draft: [], batch: null, busy: false}) {
  const sample = items.find(s => s.id === allocation.sampleId) || null;
  return `<div class="split"><section><div class="section-top"><h2>待移交样本</h2><span>${items.length} 批 / 台账</span></div>${table(['样本编号 / 批次','可用量','当前位置','分装'], sampleRows(items, allocation.sampleId))}<div class="notice">台账保留真实可用量；分装预览只做方案，确认后才写入子样本并停止原样本接单。</div></section><section class="form-panel"><span class="eyebrow">REGISTER SAMPLE</span><h2>登记新样本批次</h2><form id="sample-form"><label>批次编号<input name="code" placeholder="QY-261117-D" required></label><label>样本名称<input name="title" placeholder="地表水复测留样" required></label><div class="fields"><label>可用份数<input name="quantity" type="number" min="1" max="100000" value="100" required></label><label>存放位置<input name="location" value="冷藏间 A-09" required></label></div><button class="primary" ${session.user.role === 'viewer' ? 'disabled' : ''}>登记样本 +</button></form><p class="help">登记后到「流转申请」指定接收实验室及移交份数。</p></section></div>
  <div id="allocation-area">${portionEditor(allocation, sample)}</div>`;
}
