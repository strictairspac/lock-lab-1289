import {allocationsApi} from '../api/allocations.js';

const blankDraft = () => [{code: '', quantity: ''}, {code: '', quantity: ''}];

export const allocationStore = {
  sampleId: null,
  draft: blankDraft(),
  batch: null,
  result: null,
  busy: false,
  choosing: false,
  editing: false,

  reset() {
    this.sampleId = null;
    this.draft = blankDraft();
    this.batch = null;
    this.result = null;
    this.busy = false;
    this.choosing = false;
    this.editing = false;
  },

  async open(sampleId) {
    if (this.sampleId !== sampleId) {
      this.sampleId = sampleId;
      this.draft = blankDraft();
      this.batch = null;
      this.result = null;
      this.editing = false;
    }
    // While the operator is re-filling the draft, keep the previous preview
    // out of the editor. Choosing another sample resets this flag.
    if (this.editing) return this.batch;
    if (this.batch && this.batch.status === 'preview') {
      await this.refresh();
      return this.batch;
    }
    if (!this.batch && !this.choosing) {
      this.choosing = true;
      try {
        const batches = await allocationsApi.list(sampleId);
        const latest = batches.find(b => b.status === 'confirmed') || batches[0] || null;
        if (this.sampleId === sampleId) this.batch = latest;
      } finally {
        this.choosing = false;
      }
    }
    return this.batch;
  },

  startOver() {
    this.batch = null;
    this.result = null;
    this.editing = true;
    this.draft = blankDraft();
  },

  requestKey(batchId) {
    const storageKey = 'locklab-alloc-key-' + batchId;
    let value = localStorage.getItem(storageKey);
    if (!value) {
      value = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
      localStorage.setItem(storageKey, value);
    }
    return value;
  },

  forgetKey(batchId) { localStorage.removeItem('locklab-alloc-key-' + batchId); },

  async preview() {
    const portions = this.draft
      .filter(row => row.code.trim())
      .map(row => ({code: row.code.trim(), quantity: Number(row.quantity)}));
    if (!this.sampleId) throw Error('先选择要分装的样本');
    if (!portions.length) throw Error('至少填写一份分装编号和数量');
    const view = await allocationsApi.preview(this.sampleId, {portions, note: ''});
    this.batch = view;
    this.result = null;
    this.editing = false;
    return view;
  },

  async refresh() {
    if (!this.batch) return null;
    this.batch = await allocationsApi.detail(this.batch.batch_id);
    return this.batch;
  },

  async confirm() {
    if (!this.batch) throw Error('请先生成分配预览');
    this.busy = true;
    try {
      const result = await allocationsApi.confirm(this.batch.batch_id, this.requestKey(this.batch.batch_id));
      this.result = result;
      this.batch = await allocationsApi.detail(this.batch.batch_id);
      this.forgetKey(this.batch.batch_id);
      return result;
    } finally {
      this.busy = false;
    }
  },
};
