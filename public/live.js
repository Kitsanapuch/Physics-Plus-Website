/* Physics Plus workspace: account and business data come from the server. */
(() => {
  'use strict';

  const TZ = 'Asia/Bangkok';
  const roleNames = { student: 'นักเรียน', teacher: 'ครูผู้สอน', admin: 'ผู้ดูแล' };
  const subjectNames = { science: 'วิทยาศาสตร์', math: 'คณิตศาสตร์', english: 'ภาษาอังกฤษ', physics: 'ฟิสิกส์', chemistry: 'เคมี', biology: 'ชีวะ' };
  const courseSubjects = { middle: [['science', 'วิทยาศาสตร์'], ['math', 'คณิตศาสตร์'], ['english', 'ภาษาอังกฤษ']], high: [['physics', 'ฟิสิกส์'], ['chemistry', 'เคมี'], ['biology', 'ชีวะ'], ['math', 'คณิตศาสตร์'], ['english', 'ภาษาอังกฤษ']] };
  const INDIVIDUAL_RATE_SATANG_PER_HOUR = 30000;
  const levelNames = { middle: 'มัธยมต้น', high: 'มัธยมปลาย' };
  const statusNames = {
    unpaid: 'ยังไม่ชำระ', partial: 'ชำระบางส่วน', paid: 'ชำระแล้ว', void: 'ยกเลิก',
    open: 'ยังไม่เสร็จ', completed: 'เสร็จแล้ว',
    pending: 'รอตรวจสอบ', approved: 'อนุมัติแล้ว', rejected: 'ไม่อนุมัติ',
    submitted: 'รอตรวจเวลาสอน', payroll_locked: 'ปิดงวดแล้ว', finalized: 'อนุมัติงวดแล้ว',
    draft: 'ร่าง', scheduled: 'ตามตาราง', cancelled: 'ยกเลิก', confirmed: 'ยืนยันแล้ว',
    pending_confirmation: 'รอยืนยันอัตรา', active: 'กำลังเรียน'
  };
  const $ = (id) => document.getElementById(id);
  const state = { me: null, csrf: '', data: null, courses: [], academic: null, classes: [], loadingClasses: false, availability: [], tasks: [], inbox: { notifications: [], unread_count: 0 }, revision: null, paymentAccount: null, paymentInvoiceId: '', sheetSyncStatus: null, individualBilling: null, individualBillingPeriod: '', individualBillingBusy: false, individualBillingError: '', nav: [], viewId: '', viewTargetId: '', homeHeading: '', homeSubtitle: '', loadingDetails: false, refreshBusy: false };
  let activeTasks = 0;
  let individualBillingRequest = 0;
  let adminClaimIds = null;
  let adminClaimPollBusy = false;
  let inboxPollBusy = false;
  let inboxInitialized = false;
  let syncPollBusy = false;
  let loadGeneration = 0;
  let sheetStatusRefreshTimer = 0;
  let displayFontLoadScheduled = false;
  function loadDisplayFontsAfterPaint() {
    if (displayFontLoadScheduled) return;
    displayFontLoadScheduled = true;
    // Keep the dashboard usable while the optional typeface is fetched.
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      const stylesheet = document.createElement('link');
      stylesheet.rel = 'stylesheet';
      stylesheet.href = 'https://fonts.googleapis.com/css2?family=Noto+Sans+Thai:wght@400;500;600;700&family=Outfit:wght@400;500;600;700&display=swap';
      document.head.append(stylesheet);
    }));
  }
  const noticeLabels = {
    payment_claim_submitted: 'มีนักเรียนส่งสลิปใหม่', payment_claim_approved: 'ผู้ดูแลยืนยันการชำระเงินแล้ว',
    payment_claim_rejected: 'ผู้ดูแลไม่อนุมัติสลิป กรุณาตรวจสอบ', payment_recorded: 'บันทึกการชำระเงินแล้ว',
    student_enrolled: 'มีการสมัครคอร์สใหม่', student_enrollment_updated: 'ข้อมูลการสมัครเรียนเปลี่ยนแปลง',
    invoice_created: 'มีใบแจ้งยอดค่าเรียนใหม่', class_session_updated: 'ตารางเรียนหรือสอนเปลี่ยนแปลง',
    class_session_cancelled: 'มีการยกคลาส', payroll_paid: 'บันทึกการจ่ายค่าจ้างแล้ว',
    task_assigned: 'มีงานใหม่ที่มอบหมายให้คุณ', task_updated: 'รายละเอียดงานของคุณเปลี่ยนแปลง',
    task_unassigned: 'งานของคุณถูกย้ายให้ผู้อื่น', task_cancelled: 'งานที่มอบหมายถูกยกเลิก',
    task_completed: 'งานที่มอบหมายเสร็จแล้ว'
  };
  function setActiveNav(targetId) {
    for (const button of document.querySelectorAll('#side-nav button[data-target], #mobile-nav button[data-target]')) {
      const active = button.dataset.target === targetId;
      button.classList.toggle('selected', active);
      if (active) button.setAttribute('aria-current', 'location');
      else button.removeAttribute('aria-current');
      if (active && button.parentElement?.id === 'mobile-nav') {
        button.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      }
    }
  }
  function animateNavSelection(targetId) {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    for (const button of document.querySelectorAll('#side-nav button[data-target], #mobile-nav button[data-target]')) {
      if (button.dataset.target !== targetId || typeof button.animate !== 'function') continue;
      const mobile = button.parentElement?.id === 'mobile-nav';
      button.animate(mobile
        ? [{ transform: 'scale(.97)' }, { transform: 'scale(1.045)' }, { transform: 'scale(1)' }]
        : [{ transform: 'translateY(0)' }, { transform: 'translateY(-3px)' }, { transform: 'translateY(0)' }],
      { duration: 340, easing: 'cubic-bezier(.22,.8,.25,1)' });
    }
  }
  function hashTarget() {
    try { return decodeURIComponent(location.hash.slice(1)); }
    catch { return ''; }
  }
  function showView(targetId, { historyMode = 'none', scroll = false, animate = false, focus = false } = {}) {
    const content = $('live-content');
    const target = targetId ? document.getElementById(targetId) : null;
    if (!content || !target || !content.contains(target)) return false;
    let section = target;
    while (section && section.parentElement !== content) section = section.parentElement;
    const paymentJourney = state.me?.role === 'student' && section?.id === 'student-payments'
      && Boolean(state.paymentInvoiceId);
    if (!section?.classList.contains('new-section') ||
        (!state.nav.some(([id]) => id === section.id) && !paymentJourney)) return false;
    const changed = state.viewTargetId !== targetId;
    state.viewId = section.id;
    state.viewTargetId = targetId;
    for (const view of content.querySelectorAll(':scope > .new-section')) view.hidden = view !== section;
    setActiveNav(section.id);
    const label = state.nav.find(([id]) => id === section.id)?.[1] || (paymentJourney ? 'ชำระค่าเรียน' : state.homeHeading);
    $('heading').textContent = section.id === 'overview' ? state.homeHeading : label;
    $('subtitle').textContent = section.id === 'overview' ? state.homeSubtitle : (section.querySelector('p.muted')?.textContent || state.homeSubtitle);
    if (historyMode !== 'none' && location.hash !== `#${targetId}`) {
      const url = `#${encodeURIComponent(targetId)}`;
      if (historyMode === 'replace') history.replaceState(null, '', url);
      else history.pushState(null, '', url);
    }
    if (scroll) {
      if (target === section) window.scrollTo({ top: 0, behavior: 'auto' });
      else (target.closest('.panel') || target).scrollIntoView({ behavior: 'auto', block: 'start' });
    }
    if (focus) {
      const panel = target.closest('.panel') || section;
      const heading = panel.querySelector('h2,h3') || section.querySelector('h2,h3') || section;
      const temporaryTabIndex = !heading.hasAttribute('tabindex');
      if (temporaryTabIndex) heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
      if (temporaryTabIndex) heading.addEventListener('blur', () => heading.removeAttribute('tabindex'), { once: true });
    }
    if (animate && changed && !window.matchMedia('(prefers-reduced-motion: reduce)').matches && typeof section.animate === 'function') {
      section.animate([{ opacity: .35, transform: 'translateY(7px)' }, { opacity: 1, transform: 'translateY(0)' }],
        { duration: 220, easing: 'cubic-bezier(.2,.75,.25,1)' });
    }
    if (section.id === 'admin-finance' && state.me?.role === 'admin' && !state.loadingDetails &&
        !state.individualBilling && !state.individualBillingBusy && !activeTasks) {
      void loadIndividualBilling();
    }
    return true;
  }
  function restoreViewFromLocation() {
    const target = hashTarget() || 'overview';
    if (!showView(target, { scroll: true, animate: true, focus: true })) showView('overview', { historyMode: 'replace', scroll: true, focus: true });
  }
  function updateAdminPaymentAlert(claims, announce = false, pendingCount = claims.length) {
    if (state.me?.role !== 'admin') return;
    const current = new Set(claims.map((claim) => String(claim.id)));
    const added = adminClaimIds ? claims.filter((claim) => !adminClaimIds.has(String(claim.id))) : [];
    adminClaimIds = current;
    const badge = $('payment-alert-badge');
    const button = $('payment-alert-button');
    if (badge && button) {
      badge.textContent = pendingCount > 99 ? '99+' : String(pendingCount);
      badge.hidden = pendingCount === 0;
      button.setAttribute('aria-label', `รายการแจ้งชำระรอตรวจ ${pendingCount} รายการ`);
      button.classList.toggle('has-alert', pendingCount > 0);
    }
    const count = $('admin-claim-count');
    if (count) count.textContent = String(pendingCount);
    if (announce && added.length) {
      const toast = $('payment-alert-toast');
      if (toast) {
        toast.querySelector('.payment-alert-text').textContent = `มีรายการแจ้งชำระใหม่ ${added.length} รายการ รอผู้ดูแลตรวจสอบ`;
        toast.hidden = false;
      }
    }
  }
  async function pollAdminPaymentClaims() {
    if (state.me?.role !== 'admin' || document.visibilityState === 'hidden' || adminClaimPollBusy || activeTasks) return;
    adminClaimPollBusy = true;
    try {
      const alerts = await api('/api/admin/payment-alerts');
      const claims = alerts.pending_payment_claims || [];
      updateAdminPaymentAlert(claims, true, Number(alerts.pending_count ?? claims.length));
    } catch (error) {
      if (error.status === 401) return;
    } finally {
      adminClaimPollBusy = false;
    }
  }
  async function pollInbox({ announce = false } = {}) {
    if (!state.me || document.hidden || inboxPollBusy) return;
    inboxPollBusy = true;
    try {
      const hadLoadedInbox = inboxInitialized;
      const previous = new Set((state.inbox.notifications || []).map((item) => item.id));
      const loaded = await api('/api/notifications');
      state.inbox = loaded;
      inboxInitialized = true;
      const badge = $('inbox-badge');
      const button = $('inbox-button');
      if (badge && button) {
        const count = Number(loaded.unread_count || 0);
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.hidden = count === 0;
        button.setAttribute('aria-label', `การแจ้งเตือนของฉัน ${count} รายการที่ยังไม่อ่าน`);
      }
      if (announce && hadLoadedInbox && (loaded.notifications || []).some((item) => !item.read_at && !previous.has(item.id))) {
        const toast = $('inbox-toast');
        if (toast) { toast.textContent = 'มีการแจ้งเตือนใหม่ในบัญชีของคุณ'; toast.hidden = false;
          window.setTimeout(() => { if (toast) toast.hidden = true; }, 5500); }
      }
    } catch (_error) {
      // Retain the last delivered notices and retry at the next interval.
    } finally { inboxPollBusy = false; }
  }

  async function pollSyncRevision() {
    if (!state.me || document.hidden || syncPollBusy || state.refreshBusy || activeTasks || $('live-dialog')?.open) return;
    if (document.activeElement?.matches?.('input,textarea,select,[contenteditable="true"]')) return;
    if (state.viewId === 'student-payments' && $('claim-slip')?.files?.length) return;
    syncPollBusy = true;
    try {
      const result = await api('/api/sync/revision');
      if (state.revision === null) state.revision = result.revision;
      else if (result.revision !== state.revision) {
        state.refreshBusy = true;
        try {
          individualBillingRequest += 1;
          state.individualBilling = null;
          state.individualBillingBusy = false;
          await load();
          state.revision = result.revision;
        }
        finally { state.refreshBusy = false; }
      }
      await pollInbox({ announce: true });
    } catch (_error) {
      // The current screen stays available while a later poll retries.
    } finally { syncPollBusy = false; }
  }
  function noticeTarget(notice) {
    const event = notice.event_type || '';
    if (event.includes('task')) return state.me.role === 'admin' ? 'admin-tasks' : 'overview';
    if (event.includes('payment_claim') || event.includes('invoice') || event.includes('payment_recorded'))
      return state.me.role === 'admin' ? 'admin-finance' : 'overview';
    if (event.includes('class') || event.includes('enrollment'))
      return state.me.role === 'admin' ? 'admin-classroom' : state.me.role === 'student' ? 'student-classes' : 'overview';
    if (event.includes('payroll') || event.includes('teaching_time'))
      return state.me.role === 'teacher' ? 'teacher-payroll' : state.me.role === 'admin' ? 'admin-payroll' : 'overview';
    return 'overview';
  }
  function showInbox() {
    const dialog = $('live-dialog');
    dialog.replaceChildren();
    dialog.classList.add('inbox-dialog');
    const heading = element('h2', '', 'การแจ้งเตือนของฉัน');
    heading.id = 'live-dialog-title';
    dialog.setAttribute('aria-labelledby', heading.id);
    dialog.append(heading,
      element('p', 'muted', 'รายการนี้แสดงเฉพาะบัญชีที่เกี่ยวข้องกับการเปลี่ยนแปลง'));
    const list = element('div', 'inbox-list');
    const notices = state.inbox.notifications || [];
    if (!notices.length) list.append(element('p', 'muted', 'ยังไม่มีการแจ้งเตือน'));
    for (const notice of notices) {
      const item = actionButton(noticeLabels[notice.event_type] || 'ข้อมูลในระบบมีการอัปเดต', 'read-inbox', notice.id,
        `inbox-item${notice.read_at ? '' : ' is-unread'}`);
      item.dataset.target = noticeTarget(notice);
      item.append(element('small', '', thaiDateTime(notice.created_at)));
      list.append(item);
    }
    dialog.append(list);
    const close = actionButton('ปิด', 'close-inbox', '', 'primary'); dialog.append(close);
    dialog.onclose = () => { dialog.removeAttribute('aria-labelledby'); dialog.classList.remove('inbox-dialog'); dialog.replaceChildren(); };
    dialog.showModal();
  }
  let studentClassPollBusy = false;
  let studentClassPollingStarted = false;
  let lastStudentClassPoll = 0;
  const moneyFormatter = new Intl.NumberFormat('th-TH', { style: 'currency', currency: 'THB' });
  const dateFormatter = new Intl.DateTimeFormat('th-TH', { timeZone: TZ, dateStyle: 'medium' });
  const dateTimeFormatter = new Intl.DateTimeFormat('th-TH', { timeZone: TZ, dateStyle: 'medium', timeStyle: 'short' });
  const hoursFormatter = new Intl.NumberFormat('th-TH', { maximumFractionDigits: 2 });
  const thaiMoney = (satang) => moneyFormatter.format(Number(satang || 0) / 100);
  const thaiDate = (date) => {
    if (!date) return '—';
    const parsed = typeof date === 'number' ? new Date(date * 1000) : new Date(/^\d{4}-\d{2}-\d{2}$/.test(date) ? `${date}T12:00:00+07:00` : date);
    return Number.isNaN(parsed.getTime()) ? '—' : dateFormatter.format(parsed);
  };
  const thaiDateTime = (epoch) => {
    if (!epoch) return '—';
    const parsed = new Date(epoch * 1000);
    return Number.isNaN(parsed.getTime()) ? '—' : dateTimeFormatter.format(parsed);
  };
  const bangkokToday = () => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
      timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit'
    }).formatToParts(new Date()).map((part) => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  };
  const lastCompletedBangkokMonth = () => {
    const [year, month] = bangkokToday().split('-').map(Number);
    return new Date(Date.UTC(year, month - 2, 1)).toISOString().slice(0, 7);
  };
  const hours = (minutes) => `${hoursFormatter.format(Number(minutes || 0) / 60)} ชม.`;
  const status = (value) => statusNames[value] || value || '—';
  const validId = (value) => {
    if (!/^[0-9a-f]{32}$/.test(String(value))) throw new Error('รหัสรายการไม่ถูกต้อง');
    return value;
  };
  const toSatang = (value, allowNegative = false) => {
    const str = String(value).trim();
    if (!(allowNegative ? /^-?\d{1,9}(?:\.\d{1,2})?$/ : /^\d{1,9}(?:\.\d{1,2})?$/).test(str)) throw new Error('กรุณากรอกจำนวนเงินเป็นบาท ไม่เกิน 2 ตำแหน่งทศนิยม');
    const negative = str.startsWith('-');
    const [baht, fraction = ''] = (negative ? str.slice(1) : str).split('.');
    const amount = Number(baht) * 100 + Number(fraction.padEnd(2, '0'));
    if (!Number.isSafeInteger(amount) || amount === 0) throw new Error('จำนวนเงินไม่ถูกต้อง');
    return negative ? -amount : amount;
  };
  const bangkokInput = (epoch) => {
    if (!epoch) return '';
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
      timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).formatToParts(new Date(epoch * 1000)).map((part) => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  };
  const bangkokISO = (value) => {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) throw new Error('กรุณาระบุวันและเวลา');
    return `${value}:00+07:00`;
  };
  const subjectLabel = (subject) => subjectNames[subject] || subject || '—';
  const personalCode = (user) => user.student_code || user.teacher_code || '—';
  const epochValue = (value) => typeof value === 'number' ? value : Date.parse(String(value || '')) / 1000;
  const classSummary = (item) => `${thaiDateTime(epochValue(item.starts_at))} – ${thaiDateTime(epochValue(item.ends_at))}`;
  const cancellationReason = (item) => item.cancellation_reason || '';

  async function api(path, method = 'GET', body = null) {
    const headers = { Accept: 'application/json' };
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      if (state.csrf) headers['X-CSRF-Token'] = state.csrf;
      if (activeTasks) feedback('กำลังดำเนินการ…', false, true);
    }
    const response = await fetch(path, { method, headers, credentials: 'same-origin', cache: 'no-store', body: body === null ? undefined : JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = payload.error?.code === 'claim_pending'
        ? 'ใบนี้มีรายการแจ้งชำระรอตรวจ กรุณาตรวจรายการแจ้งชำระก่อนบันทึกรับเงินโดยตรง'
        : payload.error?.message || `Server error (${response.status})`;
      const error = new Error(message);
      error.code = payload.error?.code || 'request_failed';
      error.status = response.status;
      if (response.status === 401 && path !== '/api/me') location.replace('index.html');
      throw error;
    }
    if (method !== 'GET' && state.me?.role === 'admin' && path !== '/api/admin/sheet-sync/retry') {
      window.clearTimeout(sheetStatusRefreshTimer);
      sheetStatusRefreshTimer = window.setTimeout(fetchSheetSyncStatus, 400);
    }
    return payload;
  }

  function paymentSlipError(file) {
    if (!(file instanceof File) || !['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) return 'กรุณาแนบสลิปชนิด JPG, PNG หรือ WebP';
    if (!file.size) return 'ไฟล์สลิปว่าง กรุณาเลือกภาพอื่น';
    if (file.size > 10 * 1024 * 1024) return 'สลิปมีขนาดใหญ่เกิน 10 MB';
    return '';
  }

  async function encodePaymentSlip(file) {
    const validationError = paymentSlipError(file);
    if (validationError) throw new Error(validationError);
    let upload = file;
    if (upload.size > 850 * 1024) {
      const url = URL.createObjectURL(file);
      try {
        const picture = new Image();
        picture.src = url;
        await picture.decode();
        for (const [maxSide, quality] of [[1800, .84], [1500, .72], [1200, .62]]) {
          const ratio = Math.min(1, maxSide / Math.max(picture.naturalWidth, picture.naturalHeight));
          const canvas = document.createElement('canvas');
          canvas.width = Math.max(1, Math.round(picture.naturalWidth * ratio));
          canvas.height = Math.max(1, Math.round(picture.naturalHeight * ratio));
          const context = canvas.getContext('2d');
          if (!context) throw new Error('ไม่สามารถอ่านภาพสลิปได้');
          context.fillStyle = '#fff';
          context.fillRect(0, 0, canvas.width, canvas.height);
          context.drawImage(picture, 0, 0, canvas.width, canvas.height);
          const compressed = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
          if (!compressed) throw new Error('ไม่สามารถย่อภาพสลิปได้');
          upload = compressed;
          if (upload.size <= 850 * 1024) break;
        }
      } finally { URL.revokeObjectURL(url); }
    }
    if (!upload.size || upload.size > 850 * 1024) throw new Error('ภาพสลิปยังใหญ่เกิน 850 KB กรุณาใช้ภาพที่มีขนาดเล็กลง');
    const bytes = new Uint8Array(await upload.arrayBuffer());
    const chunks = [];
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 0x8000)));
    }
    return { slip_mime: upload.type, slip_base64: btoa(chunks.join('')) };
  }

  async function exportReport() {
    const dataset = $('report-kind').value;
    if (!/^(students|teachers|courses|classes|class_rosters|enrollments|invoices|payments|receipts|claims|teaching|payroll|rates|teacher_availability|assignments)$/.test(dataset)) throw new Error('ชุดข้อมูลไม่ถูกต้อง');
    const rows = [];
    let page = 1;
    do {
      const result = await api(`/api/admin/reports/${dataset}?page=${page}`);
      rows.push(...result.rows);
      page = result.next_page;
    } while (page);
    if (!rows.length) throw new Error('ยังไม่มีข้อมูลในชุดนี้');
    const columns = Object.keys(rows[0]);
    const quote = (value) => {
      let text = String(value ?? '');
      if (/^[\s]*[=+\-@]/.test(text)) text = `'${text}`;
      return `"${text.replace(/"/g, '""')}"`;
    };
    const csv = '\uFEFF' + [columns.map(quote).join(','), ...rows.map((row) => columns.map((column) => quote(row[column])).join(','))].join('\r\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url; link.download = `physics-plus-${dataset}-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    return rows.length;
  }

  function element(tag, className = '', content = '') {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== '') node.textContent = String(content);
    return node;
  }
  function eyeIcon() {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const outline = document.createElementNS(ns, 'path');
    outline.setAttribute('d', 'M2 12s3.7-6 10-6 10 6 10 6-3.7 6-10 6-10-6-10-6Z');
    const pupil = document.createElementNS(ns, 'circle');
    pupil.setAttribute('cx', '12'); pupil.setAttribute('cy', '12'); pupil.setAttribute('r', '2.5');
    const slash = document.createElementNS(ns, 'path');
    slash.setAttribute('d', 'M3 3 21 21'); slash.setAttribute('class', 'eye-slash');
    svg.append(outline, pupil, slash);
    return svg;
  }
  function cell(row, value, className = '') {
    const node = element('td', className, value);
    row.append(node);
    return node;
  }
  function emptyRow(body, columns, message) {
    const row = element('tr');
    cell(row, message).colSpan = columns;
    body.append(row);
  }
  function actionButton(label, action, id, className = '') {
    const button = element('button', className, label);
    button.type = 'button';
    button.dataset.action = action;
    if (id) button.dataset.id = id;
    return button;
  }
  function option(select, value, label) {
    const node = element('option', '', label);
    node.value = value;
    select.append(node);
  }
  function focusPanel(title, detail, actionLabel = '', target = '') {
    const panel = element('section', 'panel focus-panel');
    const copy = element('div');
    copy.append(element('span', 'focus-eyebrow', 'สิ่งที่ควรรู้ตอนนี้'));
    copy.append(element('h2', '', title));
    copy.append(element('p', 'muted', detail));
    panel.append(copy);
    if (actionLabel && target) {
      const button = actionButton(actionLabel, 'jump', '', 'focus-link');
      button.dataset.target = target;
      panel.append(button);
    }
    return panel;
  }
  function queueItem(count, title, detail, target, urgent = false) {
    const card = element('article', `queue-item${urgent && count ? ' is-urgent' : ''}`);
    const info = element('div');
    info.append(element('strong', 'queue-count', count));
    info.append(element('h3', '', title));
    info.append(element('p', 'muted', detail));
    const button = actionButton('เปิดดูรายการ', 'jump', '', 'queue-link');
    button.dataset.target = target;
    card.append(info, button);
    return card;
  }
  function workloadPanel(items) {
    const colors = ['#efc62f', '#5a82cc', '#6c9e85', '#bd84a3', '#d88668'];
    const counts = items.map(([label, value]) => [label, Math.max(0, Math.floor(Number(value) || 0))]);
    const total = counts.reduce((sum, [, value]) => sum + value, 0);
    const panel = element('section', 'panel workload-panel');
    const heading = element('div', 'workload-heading');
    heading.append(element('span', 'workload-eyebrow', 'OPERATIONS SNAPSHOT'));
    heading.append(element('h2', '', 'ภาพรวมงานที่ต้องจัดการ'));
    heading.append(element('p', 'muted', 'สัดส่วนจากจำนวนรายการล่าสุดในระบบ'));
    const content = element('div', 'workload-content');
    const ring = element('div', 'workload-ring');
    let position = 0;
    const slices = [];
    counts.forEach(([, value], index) => {
      if (!value || !total) return;
      const end = position + value / total * 100;
      slices.push(`${colors[index]} ${position}% ${end}%`);
      position = end;
    });
    ring.style.background = slices.length ? `conic-gradient(${slices.join(',')})` : '#efede4';
    ring.setAttribute('role', 'img');
    ring.setAttribute('aria-label', `งานที่ต้องจัดการทั้งหมด ${total} รายการ`);
    const ringCopy = element('div', 'workload-ring-copy');
    ringCopy.append(element('strong', '', total), element('small', '', 'รายการทั้งหมด'));
    ring.append(ringCopy);
    const breakdown = element('div', 'workload-breakdown');
    counts.forEach(([label, value], index) => {
      const row = element('div', 'workload-row');
      const caption = element('div', 'workload-row-caption');
      const swatch = element('span', 'workload-swatch');
      swatch.style.backgroundColor = colors[index];
      caption.append(swatch, element('span', '', label), element('strong', '', value));
      const track = element('div', 'workload-track');
      const bar = element('span', 'workload-bar');
      bar.style.width = total ? `${value / total * 100}%` : '0%';
      bar.style.backgroundColor = colors[index];
      track.append(bar); row.append(caption, track); breakdown.append(row);
    });
    content.append(ring, breakdown); panel.append(heading, content);
    return panel;
  }
  function feedback(message, isError = false, isPending = false) {
    const box = $('live-feedback');
    if (!box) return;
    box.textContent = message;
    box.classList.toggle('is-error', isError);
    box.classList.toggle('is-pending', isPending);
    box.hidden = !message;
  }
  async function task(control, work, success = '') {
    activeTasks += 1;
    if (control) {
      control.disabled = true;
      control.classList.add('is-working');
      control.setAttribute('aria-busy', 'true');
    }
    feedback('');
    try {
      await work();
      if (success) feedback(success);
    } catch (error) {
      feedback(error.message || 'เกิดข้อผิดพลาด กรุณาลองอีกครั้ง', true);
    } finally {
      activeTasks -= 1;
      if (control) {
        control.disabled = false;
        control.classList.remove('is-working');
        control.removeAttribute('aria-busy');
      }
    }
  }

  function shell(user) {
    const role = user.role;
    document.body.classList.remove('loading-screen');
    document.body.innerHTML = `
      <header class="live-header workspace-topbar"><div class="brand"><img class="mark" src="physics-plus-logo-yellow-black.jpg" alt="โลโก้ Physics Plus"><div>Physics Plus<div class="muted" id="header-role"></div></div></div><nav class="side-nav" id="side-nav" aria-label="ส่วนต่าง ๆ ของระบบ"></nav><div class="side-profile workspace-profile"><span class="profile-avatar" id="profile-avatar" aria-hidden="true"></span><div><strong id="profile-name"></strong><small id="profile-role"></small></div></div><div class="header-actions"><button type="button" id="password-button">เปลี่ยนรหัสผ่าน</button><button type="button" id="logout-button">ออกจากระบบ</button></div></header>
      <nav class="mobile-nav" id="mobile-nav" aria-label="ส่วนต่าง ๆ ของระบบ"></nav>
      <main><div class="intro"><div><div class="eyebrow">PHYSICS PLUS / LEARNING SPACE</div><h1 id="heading" aria-live="polite"></h1><p class="muted" id="subtitle"></p></div><span class="badge" id="hero-badge"></span><span class="math m1" aria-hidden="true">E = mc²</span><span class="math m2" aria-hidden="true">∫ x dx</span><span class="math m3" aria-hidden="true">F = ma</span><img class="hero-emblem" src="physics-plus-logo-yellow-black.jpg" alt="ภาพไอน์สไตน์บนโลโก้ Physics Plus"></div><p id="live-feedback" class="live-feedback" role="status" aria-live="polite" hidden></p><div id="student-live-cancellation" class="class-live-cancellation" role="alert" hidden></div><div id="live-content"></div></main>
      <dialog id="live-dialog" class="live-dialog"></dialog>`;
    $('profile-name').textContent = user.name;
    const inboxButton = element('button', 'inbox-button');
    inboxButton.type = 'button'; inboxButton.id = 'inbox-button';
    inboxButton.dataset.action = 'open-inbox';
    inboxButton.innerHTML = '<span aria-hidden="true">✉</span><span id="inbox-badge" class="payment-alert-badge" hidden>0</span>';
    inboxButton.setAttribute('aria-label', 'การแจ้งเตือนของฉัน');
    document.querySelector('.header-actions').prepend(inboxButton);
    const inboxToast = element('aside', 'inbox-toast');
    inboxToast.id = 'inbox-toast'; inboxToast.hidden = true;
    inboxToast.setAttribute('role', 'status');
    document.body.append(inboxToast);
    if (role === 'admin') {
      const alertButton = element('button', 'payment-alert-button');
      alertButton.type = 'button'; alertButton.id = 'payment-alert-button';
      alertButton.dataset.action = 'open-payment-alerts';
      alertButton.innerHTML = '<span aria-hidden="true">🔔</span><span id="payment-alert-badge" class="payment-alert-badge" hidden>0</span>';
      alertButton.setAttribute('aria-label', 'รายการแจ้งชำระรอตรวจ 0 รายการ');
      document.querySelector('.header-actions').prepend(alertButton);
      const toast = element('aside', 'payment-alert-toast');
      toast.id = 'payment-alert-toast'; toast.hidden = true;
      toast.setAttribute('role', 'alert');
      toast.innerHTML = '<span class="payment-alert-text"></span><button type="button" data-action="open-payment-alerts">เปิดดูรายการ</button><button type="button" data-action="dismiss-payment-alert" aria-label="ปิดแจ้งเตือน">×</button>';
      document.body.append(toast);
    }
    $('profile-avatar').textContent = user.name.trim().slice(0, 1) || 'PP';
    $('profile-role').textContent = roleNames[role];
    $('header-role').textContent = roleNames[role];
    $('hero-badge').textContent = `${personalCode(user) !== '—' ? `${personalCode(user)} · ` : ''}${roleNames[role]}`;
    const headlines = {
      student: ['พื้นที่เรียนรู้ของคุณ', 'คอร์สที่เรียนและยอดค่าเรียนของคุณอยู่ที่นี่'],
      teacher: ['จัดการคาบสอนในที่เดียว', 'ดูตารางสอน ลงเวลาจริง และติดตามค่าจ้างที่อนุมัติ'],
      admin: ['ดูแล Physics Plus', 'จัดการคอร์ส ค่าเรียน เวลาสอน และการจ่ายค่าจ้าง']
    };
    state.homeHeading = headlines[role][0];
    state.homeSubtitle = headlines[role][1];
    $('heading').textContent = state.homeHeading;
    $('subtitle').textContent = state.homeSubtitle;
    const nav = role === 'student'
      ? [['overview', 'ภาพรวม'], ['student-classes', 'ห้องเรียนของฉัน'], ['catalog', 'สมัครเรียน']]
      : role === 'teacher'
        ? [['overview', 'คาบสอน'], ['teacher-time', 'ลงเวลาสอน'], ['teacher-payroll', 'ค่าจ้างของฉัน']]
        : [['overview', 'ภาพรวม'], ['admin-staff', 'ครูผู้สอน'], ['admin-accounts', 'บัญชีนักเรียน'], ['admin-classroom', 'ห้องเรียน'], ['admin-tasks', 'งานที่มอบหมาย'], ['admin-enrollments', 'การสมัครเรียน'], ['admin-finance', 'ค่าเรียน'], ['admin-teaching', 'เวลาสอน'], ['admin-rates', 'อัตราค่าจ้าง'], ['admin-management', 'รายวิชา'], ['admin-payroll', 'จ่ายค่าจ้าง'], ['admin-reports', 'Google Sheet']];
    state.nav = nav;
    for (const [target, label] of nav) {
      for (const container of [$('side-nav'), $('mobile-nav')]) {
        const button = actionButton(label, 'jump');
        button.dataset.target = target;
        container.append(button);
      }
    }
    setActiveNav(nav[0][0]);
    window.addEventListener('popstate', restoreViewFromLocation);
    window.addEventListener('hashchange', restoreViewFromLocation);
    if (role === 'admin') {
      window.setInterval(pollAdminPaymentClaims, 30000);
      document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') pollAdminPaymentClaims(); });
      window.setInterval(() => { if (!document.hidden) fetchSheetSyncStatus(); }, 30000);
      document.addEventListener('visibilitychange', () => { if (!document.hidden) fetchSheetSyncStatus(); });
    }
    window.setInterval(() => { void pollSyncRevision(); }, 30000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void pollSyncRevision(); });
    $('logout-button').addEventListener('click', (event) => task(event.currentTarget, async () => {
      await api('/api/auth/logout', 'POST', {});
      location.replace('index.html');
    }));
    $('password-button').addEventListener('click', () => passwordDialog(false));
    document.addEventListener('click', delegatedClick);
    document.addEventListener('submit', delegatedSubmit);
  }

  function formLayout() {
    const role = state.me.role;
    if (role === 'student') return `
      <section id="overview" class="new-section"><div class="stats"><div class="stat"><span>ยอดค้างชำระ</span><strong id="student-outstanding">—</strong><small>จากใบแจ้งยอดของคุณ</small></div><div class="stat"><span>คอร์สที่ลงเรียน</span><strong id="student-course-count">—</strong><small>ข้อมูลจากบัญชีของคุณ</small></div><div class="stat"><span>รายการแจ้งชำระรอตรวจ</span><strong id="student-claim-count">—</strong><small>ผู้ดูแลจะตรวจหลักฐานอ้างอิง</small></div></div><section class="panel"><div class="panel-head"><div><h2>คอร์สและใบแจ้งยอดของฉัน</h2><p class="muted">ยอดคงเหลือคำนวณจากรายการชำระที่ผู้ดูแลบันทึกแล้ว</p></div></div><div class="tablewrap"><table><thead><tr><th>คอร์ส</th><th>เลขที่ใบแจ้งยอด</th><th>ยอดทั้งหมด</th><th>คงเหลือ</th><th>ครบกำหนด</th><th>สถานะ</th></tr></thead><tbody id="student-enrollments"></tbody></table></div></section></section>
      <section id="student-classes" class="panel new-section"><div class="panel-head"><div><h2>ห้องเรียนและตารางเรียนของฉัน</h2><p class="muted">ผู้ดูแลเป็นผู้จัดห้อง ครู เวลา และรายชื่อทุกคน หลังจัดเข้าห้องแล้วจะแสดงที่นี่</p></div></div><div id="student-class-alerts" class="class-alert-list" aria-live="polite"></div><div class="tablewrap"><table><thead><tr><th>รหัสวิชา / คอร์ส</th><th>ห้อง</th><th>วันและเวลา</th><th>ผู้สอน</th><th>สถานะ</th></tr></thead><tbody id="student-classes-rows"></tbody></table></div></section>
      <section id="catalog" class="panel new-section"><div class="panel-head"><div><h2>สมัครเรียน</h2><p class="muted">คอร์สกลุ่มแสดงยอดและพาไปชำระหลังยืนยันซื้อ ส่วนเรียนเดี่ยวคิด 300 บาทต่อชั่วโมงที่สอนจริง และสรุปยอดเมื่อสิ้นเดือน</p></div></div><div class="course-grid" id="course-grid"></div></section>
      <section id="student-payments" class="new-section"><section class="panel payment-invoice-panel" id="payment-list"><div class="panel-head"><div><h2>รายการค่าเรียนที่ต้องชำระ</h2><p class="muted">กดเข้ารายการเพื่อดูยอดและบัญชีรับโอนที่ผู้ดูแลกำหนด</p></div></div><div id="payment-invoice-list" class="payment-invoice-list"></div></section><section id="payment-detail" class="payment-detail" hidden><button type="button" class="payment-back" data-action="payment-back">← กลับหน้าภาพรวม</button><section class="panel"><p class="eyebrow">รายละเอียดการชำระเงิน</p><h2 id="payment-detail-title">—</h2><p id="payment-detail-invoice" class="muted"></p><div class="payment-due"><span>ยอดที่ต้องชำระ</span><strong id="claim-display-amount">—</strong></div><div id="payment-account-slot"></div><form data-action="student-claim" class="live-form payment-slip-form"><input type="hidden" name="invoice_id" id="claim-invoice"><label>แนบสลิปการโอนเงิน<input name="slip" id="claim-slip" type="file" accept="image/jpeg,image/png,image/webp" required></label><p class="foot">รองรับ JPG, PNG หรือ WebP ระบบจะย่อภาพให้เหมาะสมก่อนส่ง ยอดและเลขอ้างอิงสร้างจากข้อมูลในระบบ ไม่ต้องกรอกเอง</p><button class="primary" type="submit">ส่งสลิปให้ผู้ดูแลตรวจสอบ</button></form></section></section><div class="grid payment-history"><section class="panel"><h2>รายการที่ส่งตรวจแล้ว</h2><div class="tablewrap"><table><thead><tr><th>วันที่</th><th>จำนวน</th><th>หลักฐาน</th><th>สถานะ</th></tr></thead><tbody id="student-claims"></tbody></table></div></section><section class="panel receipt-panel"><h2>ใบสำคัญรับเงินของฉัน</h2><p class="muted">ออกตามคอร์สและยอดที่ผู้ดูแลยืนยันรับจริง</p><div class="tablewrap"><table><thead><tr><th>เลขที่ใบสำคัญ</th><th>คอร์ส / ใบแจ้งยอด</th><th>วันที่รับเงิน</th><th>จำนวนที่รับ</th><th>เอกสาร</th></tr></thead><tbody id="student-receipts"></tbody></table></div></section></div></section>`;
    if (role === 'teacher') return `
      <section id="overview" class="new-section"><div class="stats"><div class="stat"><span>คาบที่ได้รับมอบหมาย</span><strong id="teacher-session-count">—</strong><small>จากตารางสอนจริง</small></div><div class="stat"><span>รายการลงเวลารอตรวจ</span><strong id="teacher-pending-count">—</strong><small>ผู้ดูแลต้องอนุมัติ</small></div><div class="stat"><span>ค่าจ้างที่จ่ายแล้ว</span><strong id="teacher-paid-total">—</strong><small>จากงวดค่าจ้างของคุณ</small></div></div><section class="panel"><div class="panel-head"><div><h2>ตารางสอนและสถานะเวลาสอน</h2><p class="muted">เลือกเช็กชื่อในคาบที่มีนักเรียน และลงเวลาหลังสอนจริง หากต้องยกคลาสล่วงหน้า ให้แจ้งเหตุผลเพื่อแจ้งนักเรียนในห้อง</p></div></div><div class="tablewrap"><table><thead><tr><th>คาบ / คอร์ส</th><th>ห้อง / เวลา</th><th>นักเรียน</th><th>สถานะ</th><th>เวลาสอน</th><th>ดำเนินการ</th></tr></thead><tbody id="teacher-sessions"></tbody></table></div></section></section>
      <section id="teacher-time" class="panel new-section"><h2>ลงเวลาสอนจริง</h2><p class="muted">เวลาอ้างอิงประเทศไทย (UTC+7) บันทึกได้หลังสอนเสร็จ และต้องใกล้คาบที่กำหนด</p><form data-action="teacher-time" class="live-form live-form-grid"><label>คาบเรียน<select name="session_id" id="time-session" required></select></label><label>เริ่มสอนจริง<input name="actual_start" id="actual-start" type="datetime-local" required></label><label>เลิกสอนจริง<input name="actual_end" id="actual-end" type="datetime-local" required></label><label>พัก (นาที)<input name="break_minutes" type="number" min="0" max="480" value="0" required></label><button type="submit" class="primary">ส่งเวลาสอนให้ตรวจ</button></form><p class="foot">ระบบจะตรวจเวลาซ้อนและคำนวณค่าจ้างหลังผู้ดูแลเช็กชื่อครบและยืนยันอัตราแล้ว</p></section>
      <section id="teacher-payroll" class="panel new-section"><h2>ประวัติค่าจ้างของฉัน</h2><p class="muted" id="teacher-approved-unpaid"></p><div class="tablewrap"><table><thead><tr><th>งวด</th><th>ยอด</th><th>สถานะ</th><th>วันที่จ่าย</th><th>รายละเอียด</th></tr></thead><tbody id="teacher-payroll-rows"></tbody></table></div></section>`;
    return `
      <section id="overview" class="new-section"><div class="stats"><div class="stat"><span>คงค้างในใบแจ้งยอดล่าสุด</span><strong id="admin-outstanding">—</strong><small>จาก 100 รายการล่าสุดที่แสดง</small></div><div class="stat"><span>รายการแจ้งชำระรอตรวจ</span><strong id="admin-claim-count">—</strong><small>ตรวจเงินก่อนยืนยัน</small></div><div class="stat"><span>เวลาสอนรอตรวจ</span><strong id="admin-time-count">—</strong><small>ต้องเช็กชื่อครบและมีอัตราที่ยืนยัน</small></div></div></section>
      <section id="admin-staff" class="panel new-section"><div class="panel-head staff-panel-head"><div><h2>ทะเบียนครูผู้สอน</h2><p class="muted">จัดการครูเหมือนทะเบียนพนักงาน ผู้ดูแลเลือกครูประจำคอร์สและตารางสอนได้แม้ครูยังไม่มีบัญชีเข้าใช้ระบบ</p></div><button type="button" class="primary" data-action="staff-add">+ เพิ่มครู</button></div><div class="staff-summary"><span id="staff-total">ครูทั้งหมด — คน</span><span id="staff-pending">รอเปิดบัญชี — คน</span></div><label class="admin-search-label" for="admin-staff-search">ค้นหาชื่อหรือรหัสครู<input id="admin-staff-search" type="search" autocomplete="off" placeholder="ค้นหารายชื่อครู"></label><div class="tablewrap"><table><thead><tr><th>รหัสครู</th><th>ชื่อครู</th><th>การเข้าใช้ระบบ</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-staff-rows"></tbody></table></div><p class="foot">การเปิดบัญชีครูต้องใช้อีเมลจริงของครู ระบบจะแสดงรหัสผ่านชั่วคราวเพียงครั้งเดียว</p></section>
      <section id="admin-accounts" class="panel new-section"><div class="panel-head"><div><h2>จัดการบัญชีนักเรียน</h2><p class="muted">สร้างบัญชี รีเซ็ตรหัสผ่านชั่วคราว และเปิดหรือปิดสิทธิ์เข้าใช้ระบบ</p></div></div><form data-action="admin-user-create" class="live-form live-form-grid"><label>ชื่อ–นามสกุล<input name="name" required maxlength="120"></label><label>อีเมล<input name="email" type="email" required autocomplete="off"></label><input name="role" type="hidden" value="student"><button type="submit" class="primary">สร้างบัญชีนักเรียน</button></form><p class="foot">รหัสผ่านชั่วคราวจะแสดงเพียงครั้งเดียว ผู้รับต้องเปลี่ยนรหัสเมื่อเข้าสู่ระบบครั้งแรก</p><label class="admin-search-label" for="admin-user-search">ค้นหาชื่อ รหัส หรืออีเมล<input id="admin-user-search" type="search" autocomplete="off" placeholder="พิมพ์เพื่อค้นหาบัญชี"></label><div class="tablewrap"><table><thead><tr><th>รหัสประจำตัว</th><th>ชื่อ / บัญชี</th><th>ประเภท</th><th>สถานะ</th><th>รหัสชั่วคราว</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-user-rows"></tbody></table></div></section>
      <section id="admin-finance" class="new-section"><section class="panel"><h2>แจ้งยอดค่าเรียนคอร์สกลุ่ม</h2><p class="muted">นักเรียนที่ยืนยันซื้อคอร์สกลุ่มจะได้รับยอดตามราคาคอร์ส คอร์สตัวต่อตัวสรุปแยกตามชั่วโมงสอนจริงเมื่อสิ้นเดือน</p><form data-action="admin-invoice" class="live-form live-form-grid"><label>นักเรียน / คอร์สกลุ่มที่ยังไม่แจ้งยอด<select name="enrollment_id" id="invoice-enrollment" required></select></label><label>ค่าเรียนที่แจ้ง (บาท)<input name="amount" id="invoice-amount" inputmode="decimal" required placeholder="เลือกคอร์สเพื่อดึงราคา"></label><label>วันครบกำหนด<input name="due_date" type="date" required></label><label>รายละเอียด<input name="description" maxlength="250" placeholder="เช่น ค่าเรียนคอร์สคณิตศาสตร์"></label><button type="submit" class="primary">แจ้งยอดให้นักเรียน</button></form><p class="foot">บัญชีรับโอนจะแสดงให้นักเรียนเฉพาะเมื่อเปิดรายละเอียดชำระเงินของคำสั่งซื้อ</p></section><div class="grid"><section class="panel"><h2>รายการแจ้งยอด</h2><div class="tablewrap"><table><thead><tr><th>เลขที่ / นักเรียน</th><th>ยอด</th><th>คงเหลือ</th><th>ครบกำหนด</th><th>สถานะ</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-invoices"></tbody></table></div></section><section class="panel" id="admin-claims"><h2>รายการแจ้งชำระรอตรวจ</h2><p class="muted">ระบบตรวจความสอดคล้องของข้อมูลสลิปเบื้องต้นเท่านั้น ผู้ดูแลต้องตรวจยอดที่เข้าบัญชีธนาคารจริงก่อนบันทึกว่า “ถูกต้อง” และยืนยันรับเงิน</p><div class="tablewrap"><table><thead><tr><th>นักเรียน / อ้างอิง</th><th>ยอด</th><th>ผลตรวจสลิป</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-claims-rows"></tbody></table></div></section></div><section class="panel"><h2>บันทึกการรับเงินโดยผู้ดูแล</h2><p class="muted">สำหรับเงินที่ตรวจสอบแล้ว กรุณาใช้เลขอ้างอิงที่ตรวจยืนยันได้</p><form data-action="admin-payment" class="live-form live-form-grid"><label>ใบแจ้งยอด<select name="invoice_id" id="payment-invoice" required></select></label><label>จำนวนที่ได้รับ (บาท)<input name="amount" id="payment-amount" inputmode="decimal" required></label><label>วิธีรับเงิน<input name="method" required maxlength="30" placeholder="เช่น bank_transfer"></label><label>เลขอ้างอิง<input name="reference" required maxlength="120"></label><button type="submit" class="primary">บันทึกรับเงิน</button></form></section></section>
      <section id="admin-teaching" class="panel new-section"><h2>ตรวจเวลาสอน</h2><p class="muted">เช็กชื่อครบทุกคนก่อนอนุมัติ ระบบจะใช้อัตราที่คุณยืนยันไว้</p><div class="tablewrap"><table><thead><tr><th>ครู / คอร์ส</th><th>เวลาสอนจริง</th><th>พัก</th><th>เข้าเรียน</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-times"></tbody></table></div></section>
      <section id="admin-rates" class="panel new-section"><h2>อัตราค่าจ้างครู</h2><p class="muted">อัตรากลางที่ยืนยันแล้วใช้คำนวณค่าจ้างทุกวิชาและทุกครูตามระดับชั้นกับจำนวนผู้เรียน 1–3, 4–6, 7–9 และ 10–13 คน หากมีข้อตกลงเฉพาะครูหรือวิชา ให้เพิ่มอัตราเฉพาะพร้อมแหล่งอ้างอิง</p><div class="tablewrap"><table><thead><tr><th>ขอบเขต / วิชา / ระดับ</th><th>ผู้เรียน</th><th>บาทต่อชั่วโมง</th><th>สถานะ</th><th>ที่มา / ข้อสังเกต</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-rates-rows"></tbody></table></div><details class="rate-create"><summary>เพิ่มอัตราค่าจ้างที่ตรวจสอบแล้ว</summary><form data-action="admin-rate-create" class="live-form live-form-grid"><label>ขอบเขต<select name="scope" id="rate-scope" required><option value="global">อัตรากลางทุกครู</option><option value="teacher">เฉพาะครู</option></select></label><label id="rate-teacher-field" hidden>ครู<select name="teacher_id" id="rate-teacher"></select></label><label id="rate-subject-field" hidden>วิชา<select name="subject" id="rate-subject" required><option value="all">ทุกวิชา</option><option value="science">วิทยาศาสตร์</option><option value="math">คณิตศาสตร์</option><option value="physics">ฟิสิกส์</option><option value="chemistry">เคมี</option><option value="biology">ชีววิทยา</option><option value="chemistry_biology">เคมี/ชีววิทยา</option><option value="english">อังกฤษ</option><option value="thai">ไทย</option><option value="social">สังคม</option><option value="other">อื่น ๆ</option></select></label><label>ระดับ<select name="level"><option value="middle">มัธยมต้น</option><option value="high">มัธยมปลาย</option></select></label><label>จำนวนนักเรียนต่ำสุด<input name="min_students" type="number" min="1" max="100" required></label><label>จำนวนนักเรียนสูงสุด<input name="max_students" type="number" min="1" max="100" required></label><label>บาทต่อชั่วโมง<input name="rate" inputmode="decimal" required></label><label>เริ่มใช้<input name="effective_from" type="date" required></label><label>สิ้นสุด (ถ้ามี)<input name="effective_to" type="date"></label><label class="wide-field">ที่มา/หลักฐานของอัตรา<input name="source_note" required maxlength="250" placeholder="เช่น แถวในชีตอัตราค่าจ้าง หรือข้อตกลงที่ยืนยันแล้ว"></label><button type="submit" class="primary">บันทึกอัตราที่ยืนยันแล้ว</button></form></details></section>
      <section id="admin-classroom" class="new-section"><section class="panel"><div class="panel-head"><div><h2>จัดห้องเรียนและตารางเรียน</h2><p class="muted">ผู้ดูแลเป็นผู้กำหนดห้อง ครู วันเวลา และรายชื่อนักเรียนทุกคน ไม่มีการจัดเข้าห้องอัตโนมัติ</p></div></div><form data-action="admin-class-create" class="live-form live-form-grid"><label>คอร์ส / รหัสวิชา<select name="course_id" id="class-course" required></select></label><label>ครูผู้สอน<select name="teacher_id" id="class-teacher" required></select></label><label>ห้องเรียน<select name="room_name" id="class-room" required></select></label><label>เริ่มเรียน (เวลาไทย)<input name="starts_at" type="datetime-local" required></label><label>เลิกเรียน (เวลาไทย)<input name="ends_at" type="datetime-local" required></label><fieldset class="class-roster-field wide-field"><legend>เลือกรายชื่อนักเรียนในห้อง</legend><p class="foot">แสดงเฉพาะผู้สมัครคอร์สที่เลือก กรุณาตรวจชื่อก่อนบันทึก</p><div id="admin-class-students" class="class-student-picker"></div></fieldset><button type="submit" class="primary">สร้างห้องเรียนตามที่จัด</button></form></section><section class="panel"><h2>ห้องเรียนทั้งหมด</h2><p class="muted">เปิดรายชื่อเพื่อดูผู้เรียน เปลี่ยนห้อง เวลา ครู หรือจัดรายชื่อใหม่ได้ก่อนคาบเริ่มและตามเงื่อนไขของระบบ</p><div class="tablewrap"><table><thead><tr><th>คอร์ส / รหัสวิชา</th><th>ห้อง</th><th>วันและเวลา</th><th>ครูผู้สอน</th><th>รายชื่อที่จัด</th><th>สถานะ</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-classes-rows"></tbody></table></div></section></section>
      <section id="admin-enrollments" class="panel new-section"><h2>จัดการการสมัครเรียน</h2><p class="muted">ผู้ดูแลเพิ่ม ยกเลิก หรือคืนสถานะการสมัครเรียนได้ การสมัครเรียนไม่จัดเข้าห้องโดยอัตโนมัติ</p><form data-action="admin-enrollment-create" class="live-form live-form-grid"><label>คอร์ส<select name="course_id" id="admin-enroll-course" required></select></label><label>นักเรียน<select name="student_id" id="admin-enroll-student" required></select></label><button type="submit" class="primary">เพิ่มนักเรียนในคอร์ส</button></form><div class="tablewrap"><table><thead><tr><th>รหัสนักเรียน / ชื่อ</th><th>คอร์ส / รหัสวิชา</th><th>สถานะ</th><th>สมัครเมื่อ</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-enrollment-rows"></tbody></table></div></section>
      <section id="admin-management" class="new-section"><section class="panel"><h2>สร้างคอร์สตามรายวิชาของร้าน</h2><p class="muted">มัธยมต้น: วิทยาศาสตร์ คณิตศาสตร์ ภาษาอังกฤษ · มัธยมปลาย: ฟิสิกส์ เคมี ชีวะ คณิตศาสตร์ ภาษาอังกฤษ ราคาคอร์สกลุ่มผู้ดูแลกำหนดเอง ส่วนเรียนเดี่ยวคิด 300 บาทต่อชั่วโมงจริง</p><form data-action="admin-course" class="live-form live-form-grid"><label>ระดับ<select name="level" id="course-level" required><option value="middle">มัธยมต้น</option><option value="high">มัธยมปลาย</option></select></label><label>รายวิชา<select name="subject" id="course-subject" required></select></label><label>ราคาคอร์ส (บาท)<input id="course-price" name="price" inputmode="decimal" required maxlength="12" aria-describedby="course-price-hint"></label><label>จำนวนนักเรียนสูงสุด<input name="capacity" type="number" min="1" max="100" required></label><label>ครูประจำคอร์ส (กำหนดภายหลังได้)<select name="teacher_id" id="course-teacher"></select></label><button type="submit" class="primary">สร้างคอร์สและรหัสวิชา</button></form><p class="foot" id="course-price-hint">ราคาคอร์สกลุ่มผู้ดูแลแก้ไขได้ เรียนเดี่ยวใช้อัตราคงที่ 300 บาทต่อชั่วโมงและออกยอดตามเวลาสอนจริงที่อนุมัติ</p></section><section class="panel"><h2>คอร์สทั้งหมด</h2><p class="muted">คอร์สที่ยังไม่กำหนดครูสมัครเรียนได้ ผู้ดูแลต้องเลือกครูเองก่อนจัดห้องเรียน</p><div class="tablewrap"><table><thead><tr><th>รหัสคอร์ส</th><th>วิชา / ระดับ</th><th>ราคา</th><th>ครูประจำ</th><th>จำนวนที่สมัคร</th><th>สถานะ</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-course-rows"></tbody></table></div></section></section>
      <section id="admin-payroll" class="panel new-section"><h2>งวดจ่ายค่าจ้างครู</h2><p class="muted">สร้างงวดจากเวลาสอนที่อนุมัติแล้ว จากนั้นตรวจยอด ปิดงวด และบันทึกเลขอ้างอิงการจ่าย</p><form data-action="admin-payroll-create" class="live-form live-form-grid"><label>ครู<select name="teacher_id" id="payroll-teacher" required></select></label><label>เดือนงวด<input name="period" type="month" required></label><button type="submit" class="primary">สร้างงวดร่าง</button></form><div class="tablewrap"><table><thead><tr><th>ครู / งวด</th><th>ยอด</th><th>สถานะ</th><th>จ่ายเมื่อ</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-payroll-rows"></tbody></table></div></section>
      <section id="admin-reports" class="panel new-section"><h2>ส่งออกข้อมูลสำหรับ Google Sheets</h2><p class="muted">เลือกชุดข้อมูลเพื่อดาวน์โหลด CSV แล้วนำเข้าใน Google Sheets ของร้าน ข้อมูลต้นฉบับอยู่ในฐานข้อมูลของระบบ</p><div class="report-controls"><label for="report-kind">ชุดข้อมูล</label><select id="report-kind"><option value="students">นักเรียนและข้อมูลผู้ปกครอง</option><option value="teachers">ครู</option><option value="courses">คอร์สเรียน</option><option value="classes">ห้องเรียนและตารางสอน</option><option value="class_rosters">รายชื่อในห้องและการเข้าเรียน</option><option value="enrollments">การสมัครเรียน</option><option value="invoices">ใบแจ้งยอด</option><option value="payments">การรับเงิน</option><option value="receipts">ใบสำคัญรับเงิน</option><option value="claims">รายการแจ้งชำระ</option><option value="teaching">เวลาสอน</option><option value="payroll">งวดค่าจ้าง</option><option value="rates">อัตราค่าจ้าง</option><option value="teacher_availability">ช่วงเวลาว่างของครู</option><option value="assignments">งานที่มอบหมาย</option></select><button type="button" class="primary" data-action="export-report">ดาวน์โหลด CSV</button></div><p class="foot">ไฟล์นักเรียนมีข้อมูลผู้ปกครอง กรุณาเก็บใน Google Sheet ที่จำกัดสิทธิ์เฉพาะผู้ดูแล</p></section>`;
  }

  function installLivePanels() {
    if (state.me.role === 'student') {
      const panel = element('section', 'panel transfer-panel');
      panel.innerHTML = '<div class="panel-head"><div><h2>บัญชีสำหรับโอนค่าเรียน</h2><p class="muted">โอนตามยอดด้านบน จากนั้นแนบสลิปให้ผู้ดูแลตรวจสอบ</p></div></div><div class="transfer-account"><div><span class="transfer-bank" id="transfer-bank">—</span><strong id="transfer-number">—</strong><span id="transfer-holder">—</span></div><button type="button" data-action="copy-payment-account">คัดลอกเลขบัญชี</button></div>';
      $('payment-account-slot').append(panel);
      // Keep receipts and review status visible from the overview while the
      // checkout section stays accessible only for a selected purchase.
      $('overview').append($('student-payments').querySelector('.payment-history'));
      const copyAmount = actionButton('คัดลอกยอด', 'copy-payment-amount', '', 'copy-payment-amount');
      $('payment-detail').querySelector('.payment-due').append(copyAmount);
      const upload = $('claim-slip');
      const drop = upload.closest('label');
      drop.classList.add('payment-drop');
      const dropIcon = element('span', 'payment-drop-icon', '↑');
      dropIcon.setAttribute('aria-hidden', 'true');
      drop.prepend(dropIcon);
      const flight = element('span', 'payment-drop-flight');
      flight.setAttribute('aria-hidden', 'true');
      drop.append(flight);
      flight.addEventListener('animationend', () => drop.classList.remove('is-shooting'));
      const fileName = element('span', 'payment-file-name', 'เลือกไฟล์หรือวางรูปสลิปที่นี่');
      fileName.setAttribute('role', 'status');
      fileName.setAttribute('aria-live', 'polite');
      drop.append(fileName);
      upload.addEventListener('change', () => {
        let file = upload.files?.[0];
        const validationError = file ? paymentSlipError(file) : '';
        if (validationError) {
          upload.value = '';
          file = null;
          feedback(validationError, true);
        } else if (file) feedback('');
        fileName.classList.toggle('is-error', Boolean(validationError));
        fileName.textContent = file ? `เลือกแล้ว: ${file.name} · กดส่งหลักฐานเพื่อยืนยัน` : 'เลือกไฟล์หรือวางรูปสลิปที่นี่';
        if (validationError) fileName.textContent = validationError;
        drop.classList.toggle('has-file', Boolean(file));
        drop.classList.remove('is-shooting');
        if (file && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
          requestAnimationFrame(() => drop.classList.add('is-shooting'));
        }
      });
      for (const type of ['dragenter', 'dragover']) drop.addEventListener(type, (event) => {
        event.preventDefault(); drop.classList.add('is-dragging');
      });
      for (const type of ['dragleave', 'drop']) drop.addEventListener(type, (event) => {
        event.preventDefault(); drop.classList.remove('is-dragging');
      });
      drop.addEventListener('drop', (event) => {
        if (!event.dataTransfer?.files?.length) return;
        upload.files = event.dataTransfer.files;
        upload.dispatchEvent(new Event('change', { bubbles: true }));
      });
      const slots = element('section', 'panel availability-panel');
      slots.innerHTML = '<div class="panel-head"><div><h2>ช่วงเวลาว่างสำหรับเรียนตัวต่อตัว</h2><p class="muted">ผู้ดูแลจัดช่วงเวลาว่างจากตารางสอนจริง และจะยืนยันวันเรียนอีกครั้งหลังสมัคร</p></div></div><div id="student-availability-list" class="availability-list"></div>';
      $('catalog').append(slots);
    } else if (state.me.role === 'admin') {
      const formatLabel = element('label', '', 'รูปแบบคอร์ส');
      const formatSelect = element('select'); formatSelect.name = 'course_format';
      option(formatSelect, 'group', 'เรียนกลุ่ม'); option(formatSelect, 'individual', 'เรียนเดี่ยว ตัวต่อตัว');
      formatLabel.append(formatSelect);
      $('course-price').closest('label').before(formatLabel);
      const targetLabel = element('label', '', 'จำนวนคาบเป้าหมายของคอร์ส');
      const targetInput = element('input'); targetInput.name = 'target_sessions';
      targetInput.type = 'number'; targetInput.min = '1'; targetInput.max = '200'; targetInput.value = '12'; targetInput.required = true;
      targetLabel.append(targetInput);
      $('course-price').closest('label').before(targetLabel);
      const priceInput = $('course-price');
      const priceLabel = priceInput.closest('label');
      const syncCourseFormat = () => {
        const individual = formatSelect.value === 'individual';
        if (individual) priceInput.value = '300.00';
        else if (priceInput.readOnly) priceInput.value = '';
        priceInput.readOnly = individual;
        priceLabel.firstChild.textContent = individual ? 'อัตราเรียนเดี่ยว (บาทต่อชั่วโมง)' : 'ราคาคอร์ส (บาท)';
        $('course-price-hint').textContent = individual
          ? 'อัตราเรียนเดี่ยว 300 บาทต่อชั่วโมงคงที่ ระบบสรุปยอดจากเวลาสอนจริงที่อนุมัติเมื่อสิ้นเดือน'
          : 'ราคาคอร์สกลุ่มผู้ดูแลกำหนดเอง และนักเรียนชำระหลังยืนยันซื้อ';
      };
      formatSelect.addEventListener('change', syncCourseFormat);
      syncCourseFormat();
      const availability = element('section', 'panel availability-panel');
      availability.innerHTML = '<div class="panel-head"><div><h2>เวลาว่างของครูสำหรับคอร์สตัวต่อตัว</h2><p class="muted">ผู้ดูแลประกาศช่วงว่างเอง ระบบตรวจคาบสอนที่ทับซ้อนก่อนบันทึก และใช้ช่วงนี้เมื่อจัดคาบเรียนเดี่ยว</p></div></div><form data-action="admin-availability" class="live-form live-form-grid"><label>ครูผู้สอน<select name="teacher_id" id="availability-teacher" required></select></label><label>เริ่มว่าง (เวลาไทย)<input name="starts_at" type="datetime-local" required></label><label>สิ้นสุด (เวลาไทย)<input name="ends_at" type="datetime-local" required></label><button type="submit" class="primary">เพิ่มช่วงเวลาว่าง</button></form><div class="tablewrap"><table><thead><tr><th>ครู</th><th>ช่วงเวลาว่าง</th><th>สถานะ</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-availability-rows"></tbody></table></div>';
      $('admin-classroom').prepend(availability);
      const tasks = element('section', 'new-section'); tasks.id = 'admin-tasks';
      tasks.innerHTML = '<section class="panel"><h2>มอบหมายงานให้นักเรียนและครู</h2><p class="muted">ผู้ดูแลเลือกบัญชีและคอร์สที่เกี่ยวข้อง งานจะแสดงเฉพาะผู้รับ และแจ้งเตือนเข้าบัญชีของผู้นั้น</p><form data-action="admin-task" class="live-form live-form-grid"><label>ผู้รับงาน<select name="assignee_id" id="task-assignee" required></select></label><label>คอร์สที่เกี่ยวข้อง (ถ้ามี)<select name="course_id" id="task-course"></select></label><label>ชื่องาน<input name="title" maxlength="160" required placeholder="เช่น แบบฝึกหัดเรื่องแรงและการเคลื่อนที่"></label><label>กำหนดส่ง<input name="due_date" type="date"></label><label class="wide-field">รายละเอียดงาน<textarea name="description" maxlength="2000" rows="3"></textarea></label><button type="submit" class="primary">มอบหมายงาน</button></form></section><section class="panel"><h2>งานทั้งหมดที่มอบหมาย</h2><div class="tablewrap"><table><thead><tr><th>งาน / คอร์ส</th><th>ผู้รับ</th><th>กำหนดส่ง</th><th>สถานะ</th><th>ดำเนินการ</th></tr></thead><tbody id="admin-task-rows"></tbody></table></div></section>';
      $('admin-classroom').after(tasks);
      const finance = $('admin-finance');
      const panel = element('section', 'panel transfer-admin-panel');
      panel.innerHTML = '<h2>บัญชีรับโอนค่าเรียน</h2><p class="muted">ข้อมูลนี้แสดงเฉพาะนักเรียนที่เข้าสู่ระบบ ผู้ดูแลแก้ไขได้เมื่อบัญชีรับเงินเปลี่ยน</p><form data-action="admin-payment-account" class="live-form live-form-grid"><label>ธนาคาร<input name="bank_name" required maxlength="80"></label><label>รหัสธนาคาร<input name="bank_code" required maxlength="20"></label><label>ชื่อบัญชี<input name="account_name" required maxlength="120"></label><label>เลขบัญชี<input name="account_number" required maxlength="30" inputmode="numeric"></label><button type="submit" class="primary">บันทึกบัญชีรับโอน</button></form>';
      finance.prepend(panel);
      const monthly = element('section', 'panel individual-billing-panel');
      monthly.id = 'individual-billing';
      monthly.innerHTML = '<div class="panel-head"><div><span class="snapshot-kicker">INDIVIDUAL • MONTHLY</span><h2>สรุปค่าเรียนตัวต่อตัวรายเดือน</h2><p class="muted">คิด 300 บาทต่อชั่วโมงจากเวลาสอนจริงที่ผู้ดูแลอนุมัติและเช็กชื่อนักเรียนว่าเข้าเรียนแล้ว ออกใบแจ้งยอดหลังสิ้นเดือนเท่านั้น</p></div></div><div class="individual-billing-controls"><label for="individual-billing-period">เดือนที่สอน<input id="individual-billing-period" type="month" required></label><button type="button" data-action="preview-individual-billing">ตรวจยอดล่าสุด</button></div><p id="individual-billing-status" class="billing-status" role="status">เลือกเดือนเพื่อตรวจยอด</p><div class="tablewrap"><table><thead><tr><th>นักเรียน / คอร์ส</th><th>คาบที่เข้าเรียน</th><th>เวลาจริง</th><th>ยอดค่าเรียน</th><th>สถานะ</th></tr></thead><tbody id="individual-billing-rows"></tbody></table></div><div id="individual-billing-blockers" class="billing-blockers" hidden></div><div class="individual-billing-footer"><strong id="individual-billing-total">รวม —</strong><button type="button" id="individual-billing-issue" data-action="issue-individual-billing" class="primary" disabled>ออกใบแจ้งยอดประจำเดือน</button></div>';
      finance.prepend(monthly);
      const periodInput = $('individual-billing-period');
      periodInput.max = lastCompletedBangkokMonth();
      periodInput.value = state.individualBillingPeriod || lastCompletedBangkokMonth();
      periodInput.addEventListener('change', () => { void loadIndividualBilling(); });
      renderIndividualBilling();
      const reports = $('admin-reports');
      reports.innerHTML = '<h2>การเชื่อมต่อ Google Sheet</h2><p class="muted">ข้อมูลที่บันทึกในระบบจะส่งเข้าชีตอัตโนมัติ ฐานข้อมูลเว็บไซต์ยังเป็นข้อมูลหลัก</p><div class="sheet-sync-status" id="sheet-sync-status" role="status">กำลังตรวจสถานะ…</div><div class="sheet-sync-actions"><a id="sheet-sync-link" target="_blank" rel="noopener noreferrer" hidden>เปิด Google Sheet</a><button type="button" data-action="retry-sheet-sync">ลองซิงก์รายการค้างอีกครั้ง</button></div><p class="foot">หากการเชื่อมต่อขัดข้อง ระบบเก็บรายการที่ต้องซิงก์ไว้และลองใหม่ตามกำหนด</p>';
    } else {
      const slots = element('section', 'panel availability-panel');
      slots.innerHTML = '<h2>เวลาว่างที่ผู้ดูแลเปิดไว้</h2><p class="muted">แสดงช่วงที่ผู้ดูแลเปิดรับคาบตัวต่อตัว</p><div id="teacher-availability-list" class="availability-list"></div>';
      $('overview').append(slots);
    }
  }

  function renderPaymentAccount() {
    const account = state.paymentAccount;
    if (state.me.role === 'student') {
      $('transfer-bank').textContent = account?.bank_name || 'ยังไม่ได้ระบุธนาคาร';
      $('transfer-number').textContent = account?.account_number || 'ยังไม่ได้ระบุเลขบัญชี';
      $('transfer-holder').textContent = account?.account_name || 'กรุณาติดต่อผู้ดูแล';
      const copyButton = document.querySelector('[data-action="copy-payment-account"]');
      if (copyButton) copyButton.disabled = !account?.account_number;
    } else if (state.me.role === 'admin') {
      const form = document.querySelector('form[data-action="admin-payment-account"]');
      if (!form) return;
      for (const key of ['bank_name', 'bank_code', 'account_name', 'account_number']) {
        form.elements.namedItem(key).value = account?.[key] || '';
      }
    }
  }

  function renderSheetSyncStatus() {
    if (state.me.role !== 'admin') return;
    const statusBox = $('sheet-sync-status');
    const link = $('sheet-sync-link');
    if (!statusBox || !link) return;
    const status = state.sheetSyncStatus;
    if (!status) {
      statusBox.textContent = 'กำลังตรวจสถานะ…';
      link.hidden = true;
      return;
    }
    if (status.request_failed) {
      statusBox.textContent = 'ตรวจสถานะการซิงก์ไม่ได้ชั่วคราว กรุณาลองใหม่';
      statusBox.classList.add('needs-setup');
      statusBox.dataset.state = 'stalled';
      link.hidden = true;
      return;
    }
    const pending = Number(status.pending || 0);
    const overdue = pending > 0 && Number(status.pending_age_seconds || 0) >= 120;
    statusBox.classList.toggle('needs-setup', !status?.configured || Boolean(status?.last_error_code) || overdue);
    statusBox.dataset.state = !status?.configured ? 'unconfigured' : (status.last_error_code || overdue ? 'stalled' : pending || !status.last_success_at ? 'pending' : 'ready');
    const lastSync = status?.last_success_at ? new Date(status.last_success_at) : null;
    const lastSyncLabel = lastSync && !Number.isNaN(lastSync.getTime())
      ? new Intl.DateTimeFormat('th-TH', { timeZone: TZ, dateStyle: 'medium', timeStyle: 'short' }).format(lastSync)
      : null;
    statusBox.textContent = !status?.configured
      ? 'ยังไม่ได้เชื่อมบัญชี Google สำหรับซิงก์ข้อมูล กรุณาตั้งค่าฝั่งเซิร์ฟเวอร์ตามคู่มือติดตั้ง'
      : status.last_error_code
        ? `บันทึกในฐานข้อมูลแล้ว · ซิงก์ชีตไม่สำเร็จ ${pending} ชุดข้อมูล · ระบบจะลองใหม่อัตโนมัติ`
        : pending
          ? `บันทึกในฐานข้อมูลแล้ว · กำลังซิงก์ Google Sheet ${pending} ชุดข้อมูล${overdue ? ' · ค้างเกิน 2 นาที กรุณาตรวจสอบ' : ''}`
          : lastSyncLabel
            ? `ซิงก์ Google Sheet ครบแล้ว · ล่าสุด ${lastSyncLabel}`
            : 'ตั้งค่าการเชื่อมต่อแล้ว · รอซิงก์ครั้งแรก';
    const url = status?.sheet_url || '';
    if (/^https:\/\/docs\.google\.com\/spreadsheets\/d\/[A-Za-z0-9_-]+(?:\/|$)/.test(url)) {
      link.href = url; link.hidden = false;
    } else {
      link.removeAttribute('href'); link.hidden = true;
    }
  }

  function monthlyPeriodLabel(period) {
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(String(period || ''))) return period || '—';
    return new Intl.DateTimeFormat('th-TH', { timeZone: TZ, month: 'long', year: 'numeric' })
      .format(new Date(`${period}-01T12:00:00+07:00`));
  }

  function renderIndividualBilling() {
    const input = $('individual-billing-period');
    const rows = $('individual-billing-rows');
    const statusBox = $('individual-billing-status');
    const blockerBox = $('individual-billing-blockers');
    const issue = $('individual-billing-issue');
    if (!input || !rows || !statusBox || !blockerBox || !issue) return;
    const period = state.individualBillingPeriod || lastCompletedBangkokMonth();
    input.value = period;
    input.max = lastCompletedBangkokMonth();
    rows.replaceChildren();
    blockerBox.replaceChildren();
    blockerBox.hidden = true;
    issue.disabled = true;
    const summary = state.individualBilling?.period === period ? state.individualBilling : null;
    if (!summary) {
      statusBox.textContent = state.individualBillingBusy ? 'กำลังตรวจคาบเรียน เวลาสอนจริง และยอดรายเดือน…'
        : state.individualBillingError || 'เลือกเดือนแล้วกดตรวจยอดล่าสุด';
      $('individual-billing-total').textContent = 'รวม —';
      emptyRow(rows, 5, 'ยังไม่มีข้อมูลสรุปของเดือนนี้');
      return;
    }
    const items = summary.items || [];
    const unissued = items.filter((item) => !item.already_issued && Number(item.minutes) > 0);
    if (!items.length) emptyRow(rows, 5, 'ไม่มีชั่วโมงเรียนตัวต่อตัวที่ต้องออกใบแจ้งยอดในเดือนนี้');
    for (const item of items) {
      const row = element('tr');
      const person = cell(row, item.student_name || 'นักเรียน');
      person.append(element('div', 'muted', item.course_title || 'เรียนตัวต่อตัว'));
      cell(row, `${Number(item.session_count || 0)} คาบ`);
      cell(row, hours(item.minutes));
      cell(row, thaiMoney(item.amount_satang));
      cell(row, item.already_issued ? 'ออกใบแจ้งยอดแล้ว' : 'รอออกใบแจ้งยอด');
      rows.append(row);
    }
    $('individual-billing-total').textContent = `ยอดที่ยังไม่ออกใบแจ้งยอด ${thaiMoney(unissued.reduce((sum, item) => sum + Number(item.amount_satang || 0), 0))}`;
    const blockers = summary.blockers || [];
    if (blockers.length) {
      blockerBox.hidden = false;
      blockerBox.append(element('strong', '', `ยังออกยอดไม่ได้: ต้องตรวจ ${blockers.length} รายการ`));
      const list = element('ul');
      for (const blocker of blockers.slice(0, 12)) list.append(element('li', '', typeof blocker === 'string' ? blocker : blocker.message || blocker.reason || 'มีคาบที่ยังไม่พร้อมสรุป'));
      if (blockers.length > 12) list.append(element('li', '', `และอีก ${blockers.length - 12} รายการ`));
      blockerBox.append(list);
    }
    issue.disabled = !summary.ready || !unissued.length || state.individualBillingBusy;
    statusBox.textContent = !summary.ready
      ? `เดือน ${monthlyPeriodLabel(period)} ยังไม่พร้อมปิดยอด กรุณาตรวจเวลาสอนและเช็กชื่อให้ครบ`
      : unissued.length
        ? `พร้อมออกใบแจ้งยอด ${unissued.length} รายการ จากอัตรา ${thaiMoney(summary.rate_satang_per_hour || INDIVIDUAL_RATE_SATANG_PER_HOUR)} ต่อชั่วโมงจริง`
        : `เดือน ${monthlyPeriodLabel(period)} ไม่มีรายการใหม่ที่ต้องออกใบแจ้งยอด`;
  }

  async function loadIndividualBilling(force = false) {
    if (state.me?.role !== 'admin' || !$('individual-billing-period')) return;
    const period = $('individual-billing-period').value || state.individualBillingPeriod || lastCompletedBangkokMonth();
    state.individualBillingPeriod = period;
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(period) || period > lastCompletedBangkokMonth()) {
      state.individualBilling = null;
      state.individualBillingError = 'เลือกเดือนที่สิ้นสุดแล้วตามเวลาไทย';
      renderIndividualBilling();
      return;
    }
    if (!force && state.individualBilling?.period === period) { renderIndividualBilling(); return; }
    const requestId = ++individualBillingRequest;
    state.individualBillingBusy = true;
    state.individualBillingError = '';
    state.individualBilling = null;
    renderIndividualBilling();
    try {
      const summary = await api(`/api/admin/individual-billing?period=${encodeURIComponent(period)}`);
      if (requestId !== individualBillingRequest) return;
      state.individualBilling = summary;
    } catch (error) {
      if (requestId !== individualBillingRequest) return;
      state.individualBillingError = error.message || 'โหลดสรุปยอดไม่ได้ กรุณาลองอีกครั้ง';
    } finally {
      if (requestId === individualBillingRequest) {
        state.individualBillingBusy = false;
        renderIndividualBilling();
      }
    }
  }

  function renderWorkspace() {
    const role = state.me.role;
    const hadView = Boolean(state.viewTargetId);
    const priorScrollY = window.scrollY;
    $('live-content').innerHTML = formLayout();
    installLivePanels();
    if (role === 'student') renderStudent();
    else if (role === 'teacher') renderTeacher();
    else renderAdmin();
    renderAvailability();
    renderDashboardSnapshot();
    renderTasks();
    if (role !== 'teacher') renderPaymentAccount();
    if (state.loadingDetails && role !== 'admin') {
      const pending = role === 'student' ? ['catalog', 'student-payments']
        : ['teacher-time', 'teacher-payroll'];
      for (const id of pending) {
        const section = $(id);
        if (!section) continue;
        section.setAttribute('aria-busy', 'true');
        section.replaceChildren(element('h2', '', state.nav.find(([view]) => view === id)?.[1] || 'ข้อมูล'),
          element('p', 'muted', 'กำลังโหลดข้อมูลล่าสุด…'));
      }
    }
    if (role === 'admin') {
      renderSheetSyncStatus();
      updateAdminPaymentAlert(state.data.pending_payment_claims || [], false,
        Number(state.data.overview_counts?.pending_claims ?? (state.data.pending_payment_claims || []).length));
      if (state.loadingDetails) {
        // The overview is already authenticated and useful. Let navigation
        // respond now, while keeping unfinished forms and rows out of view.
        for (const section of $('live-content').querySelectorAll(':scope > .new-section:not(#overview)')) {
          const label = state.nav.find(([id]) => id === section.id)?.[1] || 'ข้อมูล';
          const panel = section.classList.contains('panel') ? section : element('section', 'panel');
          if (panel !== section) section.replaceChildren(panel);
          else panel.replaceChildren();
          panel.setAttribute('aria-busy', 'true');
          panel.append(element('h2', '', label), element('p', 'muted', 'กำลังโหลดข้อมูลล่าสุด…'));
        }
      }
    }
    const target = state.viewTargetId || hashTarget() || 'overview';
    if (!showView(target, { historyMode: hadView ? 'none' : 'replace', scroll: !hadView })) showView('overview', { historyMode: 'replace', scroll: !hadView });
    if (hadView) window.scrollTo({ top: priorScrollY, behavior: 'auto' });
  }

  function renderAvailability() {
    const slots = state.availability || [];
    if (state.me.role === 'admin') {
      const teacherSelect = $('availability-teacher');
      option(teacherSelect, '', 'เลือกครู');
      for (const teacher of (state.data.users || []).filter((person) => person.role === 'teacher')) {
        option(teacherSelect, teacher.id, `${teacher.teacher_code || '—'} · ${teacher.name}`);
      }
      const body = $('admin-availability-rows');
      if (!slots.length) emptyRow(body, 4, 'ยังไม่ได้ประกาศช่วงเวลาว่างของครู');
      for (const slot of slots) {
        const row = element('tr');
        cell(row, `${slot.teacher_code || '—'} · ${slot.teacher_name}`);
        cell(row, `${thaiDateTime(slot.starts_at)} – ${thaiDateTime(slot.ends_at)}`);
        cell(row, slot.booked ? 'มีคาบสอนแล้ว' : 'ว่าง');
        const actions = cell(row, '');
        if (!slot.booked) actions.append(actionButton('นำออก', 'availability-cancel', slot.id));
        body.append(row);
      }
      return;
    }
    const container = $(state.me.role === 'student' ? 'student-availability-list' : 'teacher-availability-list');
    if (!container) return;
    if (state.loadingDetails) {
      container.append(element('p', 'muted', 'กำลังโหลดช่วงเวลาว่าง…'));
      return;
    }
    if (!slots.length) container.append(element('p', 'muted', 'ยังไม่มีช่วงเวลาว่างที่ผู้ดูแลประกาศ'));
    for (const slot of slots.slice(0, 12)) {
      const card = element('article', 'availability-card');
      card.append(element('strong', '', `${slot.teacher_name} · ${slot.teacher_code || '—'}`),
        element('span', '', `${thaiDateTime(slot.starts_at)} – ${thaiDateTime(slot.ends_at)}`));
      container.append(card);
    }
  }

  function renderTasks() {
    if (state.me.role !== 'admin') return;
    const assignee = $('task-assignee');
    const course = $('task-course');
    const body = $('admin-task-rows');
    if (!assignee || !course || !body) return;
    option(assignee, '', 'เลือกนักเรียนหรือครู');
    for (const person of (state.data.users || []).filter((item) =>
      ['student', 'teacher'].includes(item.role) && item.status === 'active' && !item.roster_pending)) {
      option(assignee, person.id, `${personalCode(person)} · ${person.name} (${roleNames[person.role]})`);
    }
    const populateCourses = () => {
      course.replaceChildren(); option(course, '', 'ไม่ผูกคอร์ส');
      const person = (state.data.users || []).find((item) => item.id === assignee.value);
      if (!person) return;
      const enrolled = new Set((state.academic?.enrollments || [])
        .filter((item) => item.student_id === person.id && item.status === 'active').map((item) => item.course_id));
      const assigned = new Set((state.classes || [])
        .filter((item) => item.teacher_id === person.id && item.status === 'scheduled').map((item) => item.course_id));
      for (const item of state.academic?.courses || []) {
        if (!item.active || !(person.role === 'student' ? enrolled.has(item.id) :
          item.teacher_id === person.id || assigned.has(item.id))) continue;
        option(course, item.id, `${item.course_code} · ${item.title}`);
      }
    };
    assignee.addEventListener('change', populateCourses);
    populateCourses();
    const tasks = state.tasks || [];
    if (!tasks.length) emptyRow(body, 5, 'ยังไม่มีงานที่มอบหมาย');
    for (const item of tasks) {
      const row = element('tr');
      const title = cell(row, item.title);
      title.append(element('div', 'muted', item.course_title || 'งานทั่วไป'));
      const recipient = cell(row, `${item.assignee_code || '—'} · ${item.assignee_name}`);
      recipient.append(element('div', 'muted', roleNames[item.assignee_role] || item.assignee_role));
      cell(row, item.due_date ? thaiDate(item.due_date) : 'ไม่ระบุ');
      cell(row, status(item.status));
      const actions = cell(row, ''); actions.className = 'row-actions';
      if (item.status === 'open') actions.append(actionButton('แก้ไข', 'task-edit', item.id),
        actionButton('เสร็จแล้ว', 'task-complete', item.id), actionButton('ยกเลิก', 'task-cancel', item.id));
      body.append(row);
    }
  }

  function renderDashboardSnapshot() {
    const overview = $('overview');
    const stats = overview?.querySelector('.stats');
    if (!stats) return;
    const role = state.me.role;
    const nowEpoch = Date.now() / 1000;
    const scheduled = (state.classes || []).filter((item) => item.status === 'scheduled');
    const upcoming = scheduled.filter((item) => Number(item.ends_at) > nowEpoch)
      .sort((a, b) => Number(a.starts_at) - Number(b.starts_at)).slice(0, 3);
    const enrolled = new Set((state.data?.enrollments || []).filter((item) => item.status === 'active').map((item) => item.course_id));
    const taught = new Set((state.classes || []).map((item) => item.course_id));
    const courses = (role === 'admin' ? state.academic?.courses || [] : state.courses || [])
      .filter((course) => course.active !== 0 &&
        (role === 'admin' || (role === 'student' ? enrolled.has(course.id) : taught.has(course.id))))
      .sort((a, b) => Number(taught.has(b.id)) - Number(taught.has(a.id))).slice(0, 6);
    const panel = element('section', 'panel dashboard-snapshot');
    const head = element('div', 'snapshot-head');
    head.append(element('span', 'snapshot-kicker', 'LIVE LEARNING PULSE'),
      element('h2', '', role === 'admin' ? 'ภาพรวมการเรียนและงานของร้าน' : role === 'teacher' ? 'ตารางสอนและความคืบหน้าของฉัน' : 'ตารางเรียนและความคืบหน้าของฉัน'),
      element('p', 'muted', 'ข้อมูลซิงก์อัตโนมัติทุก 30 วินาทีเมื่อเปิดหน้านี้'));
    panel.append(head);
    const grid = element('div', 'snapshot-grid');
    const schedule = element('div', 'snapshot-block');
    schedule.append(element('h3', '', role === 'student' ? 'ตารางเรียนถัดไป' : 'ตารางสอนถัดไป'));
    if (!upcoming.length) schedule.append(element('p', 'muted', state.loadingClasses
      ? 'กำลังโหลดตารางเรียนและตารางสอน…' : 'ผู้ดูแลยังไม่ได้กำหนดคาบถัดไป'));
    for (const classroom of upcoming) {
      const row = element('article', 'snapshot-schedule-row');
      row.append(element('strong', '', classroom.course_title),
        element('span', '', `${thaiDateTime(classroom.starts_at)} · ${classroom.room_name}`),
        element('small', '', role === 'student' ? `ครู ${classroom.teacher_name}` : `${classroom.roster_count || 0} คน`));
      schedule.append(row);
    }
    const progress = element('div', 'snapshot-block');
    progress.append(element('h3', '', role === 'teacher' ? 'ความคืบหน้าการสอน' : 'ความคืบหน้าคอร์ส'));
    if (!courses.length) progress.append(element('p', 'muted',
      state.loadingDetails ? 'กำลังโหลดความคืบหน้าคอร์ส…' : 'ยังไม่มีคอร์สที่จัดตารางเรียน'));
    for (const course of courses) {
      const completed = scheduled.filter((item) => item.course_id === course.id &&
        Number(item.ends_at) <= nowEpoch &&
        ['approved', 'payroll_locked', 'paid'].includes(item.time_status) &&
        (role !== 'student' || item.student_attendance === 'present')).length;
      const target = Math.max(1, Number(course.target_sessions) || 12);
      const percent = Math.min(100, Math.round(completed / target * 100));
      const row = element('div', 'snapshot-progress-row');
      const caption = element('div', 'snapshot-progress-caption');
      caption.append(element('span', '', course.title), element('strong', '', `${percent}%`));
      const bar = element('div', 'snapshot-progress-track');
      const fill = element('span', 'snapshot-progress-fill'); fill.style.width = `${percent}%`;
      bar.append(fill);
      row.append(caption, bar, element('small', 'muted', `${completed} จาก ${target} คาบเป้าหมาย`));
      progress.append(row);
    }
    const assignedTasks = element('div', 'snapshot-block snapshot-tasks');
    const openTasks = (state.tasks || []).filter((item) => item.status === 'open');
    assignedTasks.append(element('h3', '', role === 'admin' ? `งานที่มอบหมายไป · ${openTasks.length} งานค้าง` : `งานที่ได้รับ · ${openTasks.length} งานค้าง`));
    if (!openTasks.length) assignedTasks.append(element('p', 'muted', state.loadingDetails
      ? 'กำลังโหลดงานที่ได้รับมอบหมาย…' : 'ยังไม่มีงานที่ต้องทำ'));
    for (const item of openTasks.slice(0, 4)) {
      const row = element('article', 'snapshot-task-row');
      const detail = element('div');
      detail.append(element('strong', '', item.title),
        element('small', 'muted', `${role === 'admin' ? `${item.assignee_name} · ` : ''}${item.course_title || 'งานทั่วไป'}${item.due_date ? ` · ส่ง ${thaiDate(item.due_date)}` : ''}`));
      row.append(detail);
      if (role !== 'admin') row.append(actionButton('เสร็จแล้ว', 'task-complete', item.id));
      assignedTasks.append(row);
    }
    if (role === 'admin' && openTasks.length) {
      const more = actionButton('จัดการงานทั้งหมด', 'jump'); more.dataset.target = 'admin-tasks';
      assignedTasks.append(more);
    }
    grid.append(schedule, progress, assignedTasks); panel.append(grid);
    stats.after(panel);
  }

  function fetchSheetSyncStatus() {
    // Sheet status is informational and must not hold up the working area.
    void api('/api/admin/sheet-sync/status').then((status) => {
      state.sheetSyncStatus = status;
      renderSheetSyncStatus();
    }).catch(() => {
      state.sheetSyncStatus = { request_failed: true };
      renderSheetSyncStatus();
    });
  }

  function setAdminDetailsBusy(busy) {
    state.loadingDetails = busy;
    const alertButton = $('payment-alert-button');
    if (alertButton) alertButton.disabled = busy;
  }

  function renderWhenIdle(generation) {
    if (generation !== loadGeneration) return;
    const editing = document.activeElement?.matches?.('input,textarea,select,[contenteditable="true"]');
    if ($('live-dialog')?.open || activeTasks || editing || $('claim-slip')?.files?.length) {
      window.setTimeout(() => renderWhenIdle(generation), 250);
      return;
    }
    renderWorkspace();
  }

  async function load({ initial = false, overviewCounts = null } = {}) {
    const role = state.me.role;
    const generation = ++loadGeneration;
    if (initial && role !== 'admin') {
      // Give the personal dashboard and timetable priority over secondary
      // reads. Starting every request together can overload a cold remote DB.
      let classResult = null;
      const classesRequest = api(role === 'student' ? '/api/student/classes' : '/api/teacher/classes')
        .then((value) => { classResult = { value }; }, (error) => { classResult = { error }; });
      const dashboard = await api(role === 'student' ? '/api/student/dashboard' : '/api/teacher/dashboard');
      if (generation !== loadGeneration) return;
      state.data = dashboard;
      state.classes = classResult?.value?.classes || [];
      state.loadingClasses = !classResult;
      state.loadingDetails = true;
      renderWorkspace();
      // Prefer the timetable, but never let a stalled timetable request keep
      // the catalog, payment account or payroll from loading at all.
      const secondaryGate = Promise.race([
        classesRequest,
        new Promise((resolve) => window.setTimeout(resolve, 750))
      ]);
      const extras = secondaryGate.then(() => role === 'student'
        ? Promise.allSettled([api('/api/courses'), api('/api/payment-account'),
          api('/api/teacher-availability'), api('/api/tasks')])
        : Promise.allSettled([api('/api/teacher/payroll'), api('/api/teacher-availability'),
          api('/api/courses'), api('/api/tasks')]));
      if (!classResult) {
        void classesRequest.then(() => {
          if (generation !== loadGeneration) return;
          state.classes = classResult.value?.classes || [];
          state.loadingClasses = false;
          renderWhenIdle(generation);
          if (classResult.error) feedback('โหลดตารางเรียนหรือสอนไม่สำเร็จ กรุณารีเฟรชเพื่อลองใหม่', true);
        });
      } else if (classResult.error) {
        feedback('โหลดตารางเรียนหรือสอนไม่สำเร็จ กรุณารีเฟรชเพื่อลองใหม่', true);
      }
      void extras.then((results) => {
        if (generation !== loadGeneration) return;
        const value = (index) => results[index].status === 'fulfilled' ? results[index].value : null;
        if (role === 'student') {
          if (value(0)) state.courses = value(0).courses || [];
          if (value(1)) state.paymentAccount = value(1).payment_account || null;
          if (value(2)) state.availability = value(2).slots || [];
          if (value(3)) state.tasks = value(3).tasks || [];
        } else {
          if (value(0)) state.data = { ...state.data, ...value(0) };
          if (value(1)) state.availability = value(1).slots || [];
          if (value(2)) state.courses = value(2).courses || [];
          if (value(3)) state.tasks = value(3).tasks || [];
        }
        state.loadingDetails = false;
        renderWhenIdle(generation);
        if (results.some((result) => result.status === 'rejected'))
          feedback('ข้อมูลบางส่วนโหลดไม่สำเร็จ กรุณารีเฟรชเพื่อลองใหม่', true);
      });
      return;
    }
    if (role === 'student') {
      const [dashboard, catalog, classes, paymentAccount, availability, tasks] = await Promise.all([api('/api/student/dashboard'), api('/api/courses'), api('/api/student/classes'), api('/api/payment-account'), api('/api/teacher-availability'), api('/api/tasks')]);
      state.data = dashboard;
      state.courses = catalog.courses || [];
      state.classes = classes.classes || [];
      state.loadingClasses = false;
      state.paymentAccount = paymentAccount.payment_account || null;
      state.availability = availability.slots || [];
      state.tasks = tasks.tasks || [];
      state.loadingDetails = false;
    } else if (role === 'teacher') {
      const [dashboard, payroll, classes, availability, catalog, tasks] = await Promise.all([api('/api/teacher/dashboard'), api('/api/teacher/payroll'), api('/api/teacher/classes'), api('/api/teacher-availability'), api('/api/courses'), api('/api/tasks')]);
      state.data = { ...dashboard, ...payroll };
      state.classes = classes.classes || [];
      state.loadingClasses = false;
      state.availability = availability.slots || [];
      state.courses = catalog.courses || [];
      state.tasks = tasks.tasks || [];
      state.loadingDetails = false;
    } else {
      // A deep link should display its selected section and a safe loading
      // placeholder as promptly as the overview does.
      const quickOverview = initial;
      const dashboardRequest = api('/api/admin/dashboard?compact=1').then((value) => ({ value }), (error) => ({ error }));
      const detailsRequest = Promise.all([
        api('/api/admin/rates'), api('/api/admin/payroll-runs'), api('/api/admin/invoices'),
        api('/api/admin/academic'), api('/api/payment-account'), api('/api/teacher-availability'), api('/api/tasks')
      ]).then((value) => ({ value }), (error) => ({ error }));
      // The small first-screen read is optional: a failure or a slower reply
      // must never hold up the existing complete workspace.
      const overviewRequest = quickOverview && !overviewCounts
        ? api('/api/admin/overview').then((value) => ({ value }), (error) => ({ error }))
        : null;
      if (quickOverview) {
        const first = overviewCounts
          ? { kind: 'overview', result: { value: overviewCounts } }
          : await Promise.race([
            overviewRequest.then((result) => ({ kind: 'overview', result })),
            Promise.all([dashboardRequest, detailsRequest]).then(() => ({ kind: 'full' }))
          ]);
        if (first.kind === 'overview' && !first.result.error) {
          state.data = { overview_counts: first.result.value, invoices: [], users: [], staff: [] };
          state.courses = [];
          state.academic = null;
          state.classes = [];
          state.paymentAccount = null;
          setAdminDetailsBusy(true);
          renderWorkspace();
          for (const button of $('overview')?.querySelectorAll('.action-queue button') || []) button.disabled = true;
          $('overview')?.append(element('p', 'muted', 'กำลังโหลดรายละเอียดรายการอื่น ๆ…'));
        }
      }
      const dashboardResult = await dashboardRequest;
      if (dashboardResult.error) throw dashboardResult.error;
      const dashboard = dashboardResult.value;
      const details = await detailsRequest;
      if (details.error) throw details.error;
      const [rates, payroll, tuition, academic, paymentAccount, availability, tasks] = details.value;
      state.data = { ...dashboard, ...rates, ...payroll, invoices: tuition.invoices || dashboard.invoices || [], receipts: tuition.receipts || [], users: academic.users || [], staff: (academic.users || []).filter((person) => person.role === 'teacher' || person.role === 'admin'), unbilled_enrollments: tuition.enrollments || [] };
      state.courses = (academic.courses || []).filter((course) => course.active);
      state.academic = academic;
      state.classes = academic.classes || [];
      state.loadingClasses = false;
      state.paymentAccount = paymentAccount.payment_account || null;
      state.availability = availability.slots || [];
      state.tasks = tasks.tasks || [];
      if (quickOverview) setAdminDetailsBusy(false);
    }
    renderWorkspace();
    if (role === 'admin') fetchSheetSyncStatus();
  }

  function renderStudent() {
    const enrollments = state.data.enrollments || [];
    const invoices = state.data.invoices || [];
    const claims = state.data.payment_claims || [];
    $('student-outstanding').textContent = thaiMoney(invoices.filter((item) => item.status !== 'void').reduce((sum, item) => sum + Math.max(0, item.amount_satang - item.paid_satang), 0));
    $('student-course-count').textContent = String(enrollments.filter((item) => item.status === 'active').length);
    $('student-claim-count').textContent = String(claims.filter((item) => item.status === 'pending').length);
    const overdue = invoices.filter((item) => item.status !== 'void' && item.due_date < bangkokToday() && item.amount_satang > item.paid_satang);
    const unbilled = enrollments.filter((item) => item.status === 'active' && item.course_format !== 'individual' && !item.invoice_id);
    const monthlyInvoices = invoices.filter((item) => item.individual_period).sort((a, b) =>
      String(b.individual_period).localeCompare(String(a.individual_period)) ||
      String(b.invoice_number || '').localeCompare(String(a.invoice_number || '')));
    const monthlyDue = monthlyInvoices.filter((item) => item.status !== 'void' && item.amount_satang > item.paid_satang);
    const latestMonthlyByEnrollment = new Map();
    for (const invoice of monthlyInvoices) {
      if (!latestMonthlyByEnrollment.has(invoice.enrollment_id)) latestMonthlyByEnrollment.set(invoice.enrollment_id, invoice);
    }
    const pendingClaims = claims.filter((item) => item.status === 'pending');
    const nowEpoch = Date.now() / 1000;
    const cancelledClasses = state.classes.filter((item) => item.status === 'cancelled').sort((a, b) => Number(b.cancelled_at || 0) - Number(a.cancelled_at || 0));
    const recentCancellations = cancelledClasses.filter((item) => Number(item.cancelled_at || 0) >= nowEpoch - 30 * 86400 || epochValue(item.ends_at) >= nowEpoch - 7 * 86400);
    let focus = null;
    if (recentCancellations.length) {
      const latest = recentCancellations[0];
      focus = focusPanel('แจ้งยกคลาสเรียน', `${latest.course_title} · ${thaiDateTime(epochValue(latest.starts_at))}${cancellationReason(latest) ? ` · เหตุผล: ${cancellationReason(latest)}` : ''}`, 'ดูตารางเรียน', 'student-classes');
    } else if (overdue.length) focus = focusPanel('ตรวจใบแจ้งยอดที่เลยกำหนด', `${overdue.length} รายการยังมียอดคงเหลือ เลือกชำระจากรายการคอร์สด้านล่าง`);
    else if (monthlyDue.length) focus = focusPanel('มีค่าเรียนตัวต่อตัวประจำเดือน', `${monthlyDue.length} ใบแจ้งยอดที่สรุปจากชั่วโมงสอนจริง กดชำระจากรายการด้านล่าง`);
    else if (unbilled.length) focus = focusPanel('มีคอร์สที่ต้องตรวจยอด', `${unbilled.length} คอร์สยังไม่มีใบแจ้งยอด กรุณาติดต่อผู้ดูแลเพื่อตรวจรายการ`);
    else if (pendingClaims.length) focus = focusPanel('กำลังตรวจสลิป', `${pendingClaims.length} รายการรอผู้ดูแลตรวจสอบ เมื่อยืนยันแล้ว ยอดคงเหลือและใบสำคัญรับเงินจะอัปเดตในบัญชีของคุณ`);
    if (focus) $('overview').querySelector('.stats').after(focus);
    const body = $('student-enrollments');
    if (!enrollments.length) emptyRow(body, 6, 'ยังไม่ได้ลงเรียนคอร์สใด');
    for (const item of enrollments) {
      const latestMonthly = item.course_format === 'individual' ? latestMonthlyByEnrollment.get(item.enrollment_id) : null;
      const invoice = item.course_format === 'individual' ? latestMonthly : item.invoice_id ? {
        id: item.invoice_id, invoice_number: item.invoice_number, amount_satang: item.amount_satang,
        paid_satang: item.paid_satang, due_date: item.due_date, status: item.invoice_status
      } : null;
      const row = element('tr');
      const courseCell = cell(row, item.title);
      if (latestMonthly) courseCell.append(element('div', 'muted', `ใบล่าสุด: ${monthlyPeriodLabel(latestMonthly.individual_period)}`));
      if (invoice && invoice.status !== 'void' && invoice.amount_satang > invoice.paid_satang &&
          !pendingClaims.some((claim) => claim.invoice_id === invoice.id)) {
        const pay = actionButton('ชำระค่าเรียน', 'open-student-payment', invoice.id, 'inline-payment-link');
        pay.disabled = state.loadingDetails;
        courseCell.append(pay);
      }
      cell(row, invoice?.invoice_number || (item.course_format === 'individual' ? 'ยังไม่มีใบแจ้งยอด' : 'รอตรวจยอด'));
      cell(row, invoice ? thaiMoney(invoice.amount_satang) : '—');
      cell(row, invoice ? thaiMoney(Math.max(0, invoice.amount_satang - invoice.paid_satang)) : '—');
      cell(row, thaiDate(invoice?.due_date));
      cell(row, invoice ? status(invoice.status) : item.course_format === 'individual' ? 'รอสรุปชั่วโมงรายเดือน' : 'รอตรวจสอบ');
      body.append(row);
    }
    if (monthlyInvoices.length) {
      const monthlyPanel = element('section', 'panel monthly-student-panel');
      monthlyPanel.append(element('h2', '', 'ประวัติค่าเรียนตัวต่อตัวรายเดือน'));
      monthlyPanel.append(element('p', 'muted', 'ยอดแต่ละเดือนมาจากชั่วโมงสอนจริงที่อนุมัติแล้ว ใบที่ค้างชำระเปิดเพื่อดูบัญชีรับโอนและแนบสลิปได้'));
      const list = element('div', 'monthly-student-list');
      for (const invoice of monthlyInvoices) {
        const card = element('div', 'monthly-student-item');
        const detail = element('div');
        detail.append(element('strong', '', invoice.description || `ค่าเรียนเดือน ${monthlyPeriodLabel(invoice.individual_period)}`));
        detail.append(element('small', 'muted', `${invoice.invoice_number} · ${monthlyPeriodLabel(invoice.individual_period)} · ${status(invoice.status)} · ครบกำหนด ${thaiDate(invoice.due_date)}`));
        const amount = element('strong', 'monthly-student-amount', thaiMoney(invoice.status === 'paid' ? invoice.amount_satang : Math.max(0, invoice.amount_satang - invoice.paid_satang)));
        const awaitingReview = pendingClaims.some((claim) => claim.invoice_id === invoice.id);
        card.append(detail, amount);
        if (invoice.status !== 'void' && invoice.amount_satang > invoice.paid_satang) {
          const pay = actionButton(awaitingReview ? 'รอตรวจสลิป' : 'ชำระเงิน', 'open-student-payment', invoice.id, 'primary');
          pay.disabled = awaitingReview || state.loadingDetails;
          card.append(pay);
        } else card.append(element('span', 'monthly-paid-state', status(invoice.status)));
        list.append(card);
      }
      monthlyPanel.append(list);
      $('overview').append(monthlyPanel);
    }
    renderStudentClassSchedule();
    const catalog = $('course-grid');
    const enrolled = new Set(enrollments.map((item) => item.course_id));
    if (!state.courses.length) catalog.append(element('p', 'muted', 'ยังไม่มีคอร์สที่เปิดรับ'));
    for (const course of state.courses) {
      const card = element('article', 'course-card');
      const tag = element('span', 'subject-tag', `${subjectLabel(course.subject)} · ${levelNames[course.level] || '—'}${course.course_format === 'individual' ? ' · ตัวต่อตัว' : ''}`);
      const title = element('h3', '', course.title);
      const teacher = element('p', course.teacher_name ? 'muted' : 'muted awaiting-teacher', course.teacher_name ? `ครูผู้สอน: ${course.teacher_name}` : 'รอผู้ดูแลจัดครูผู้สอน');
      const price = element('strong', 'course-price', course.course_format === 'individual' ? `${thaiMoney(INDIVIDUAL_RATE_SATANG_PER_HOUR)} / ชั่วโมง` : thaiMoney(course.price_satang));
      const note = element('p', 'muted', course.course_format === 'individual' ? 'ผู้ดูแลจัดครูและเวลา คิดเงินเฉพาะชั่วโมงสอนจริง สรุปยอดหลังสิ้นเดือน' : 'ยืนยันแล้วระบบจะแสดงยอดที่ต้องโอนทันที');
      const seats = element('p', 'muted', course.course_format === 'individual' ? `ที่รับสมัคร ${course.enrolled_count}/${course.capacity} คน · เรียนแยกคาบ` : `รับ ${course.enrolled_count}/${course.capacity} คน`);
      const button = actionButton(enrolled.has(course.id) ? 'ลงเรียนแล้ว' : course.enrolled_count >= course.capacity ? 'เต็มแล้ว' : 'สมัครคอร์สนี้', 'student-enroll', course.id, 'primary');
      button.disabled = enrolled.has(course.id) || course.enrolled_count >= course.capacity;
      card.append(tag, element('small', 'record-code', course.course_code || ''), title, teacher, price, note, seats, button); catalog.append(card);
    }
    renderStudentPaymentInvoices(invoices, pendingClaims);
    const claimBody = $('student-claims');
    if (!claims.length) emptyRow(claimBody, 4, 'ยังไม่มีรายการแจ้งชำระ');
    for (const claim of claims) {
      const row = element('tr');
      cell(row, thaiDateTime(claim.created_at)); cell(row, thaiMoney(claim.amount_satang));
      cell(row, claim.reference.startsWith('SLIP-') ? 'แนบสลิปแล้ว' : 'รายการเดิม'); cell(row, status(claim.status)); claimBody.append(row);
    }
    const receiptBody = $('student-receipts');
    const receipts = state.data.receipts || [];
    if (!receipts.length) emptyRow(receiptBody, 5, 'ยังไม่มีใบสำคัญรับเงิน เมื่อผู้ดูแลยืนยันการรับเงิน เอกสารจะปรากฏที่นี่');
    for (const receipt of receipts) {
      const row = element('tr');
      cell(row, receipt.receipt_number, 'record-code');
      const purchase = cell(row, receipt.course_title || receipt.description || 'ค่าเรียน');
      if (receipt.course_code) purchase.prepend(element('div', 'record-code', receipt.course_code));
      purchase.append(element('div', 'muted', receipt.invoice_number));
      cell(row, thaiDateTime(receipt.paid_at));
      cell(row, thaiMoney(receipt.amount_satang));
      const link = element('a', 'receipt-download', 'ดาวน์โหลด PDF');
      link.href = `/api/receipts/${validId(receipt.id)}.pdf`;
      link.download = `${receipt.receipt_number}.pdf`;
      link.setAttribute('aria-label', `ดาวน์โหลดใบสำคัญรับเงิน ${receipt.receipt_number}`);
      cell(row, '').append(link);
      receiptBody.append(row);
    }
  }

  function renderStudentPaymentInvoices(invoices, pendingClaims) {
    const list = $('payment-invoice-list');
    list.replaceChildren();
    const pendingIds = new Set(pendingClaims.map((claim) => claim.invoice_id));
    const available = invoices.filter((invoice) => invoice.status !== 'void' && invoice.amount_satang > invoice.paid_satang);
    if (!available.length) list.append(element('p', 'muted', 'ตอนนี้ยังไม่มีค่าเรียนที่ต้องชำระ'));
    for (const invoice of available) {
      const card = element('article', 'payment-invoice-card');
      const detail = element('div');
      detail.append(element('span', 'record-code', invoice.invoice_number),
        element('h3', '', invoice.description || 'ค่าเรียน'),
        element('p', 'muted', `ครบกำหนด ${thaiDate(invoice.due_date)}`));
      const amount = element('strong', 'payment-invoice-amount', thaiMoney(invoice.amount_satang - invoice.paid_satang));
      const button = actionButton(pendingIds.has(invoice.id) ? 'รอตรวจสลิป' : 'ชำระเงิน', 'open-student-payment', invoice.id, 'primary');
      button.disabled = pendingIds.has(invoice.id);
      card.append(detail, amount, button); list.append(card);
    }
    const selected = available.find((invoice) => invoice.id === state.paymentInvoiceId && !pendingIds.has(invoice.id));
    if (selected) showStudentPayment(selected.id);
    else {
      state.paymentInvoiceId = '';
      $('payment-detail').hidden = true;
      $('payment-list').hidden = false;
    }
  }

  function clearStudentSlip() {
    const upload = $('claim-slip');
    if (!upload) return;
    upload.value = '';
    const drop = upload.closest('.payment-drop');
    drop?.classList.remove('has-file', 'is-shooting', 'is-dragging');
    const label = drop?.querySelector('.payment-file-name');
    if (label) {
      label.classList.remove('is-error');
      label.textContent = 'เลือกไฟล์หรือวางรูปสลิปที่นี่';
    }
  }

  function showStudentPayment(invoiceId) {
    const invoice = (state.data?.invoices || []).find((item) => item.id === invoiceId && item.status !== 'void' && item.amount_satang > item.paid_satang);
    if (!invoice || (state.data.payment_claims || []).some((claim) => claim.invoice_id === invoiceId && claim.status === 'pending')) throw new Error('รายการนี้ยังไม่พร้อมชำระ');
    if (state.paymentInvoiceId !== invoiceId) clearStudentSlip();
    state.paymentInvoiceId = invoiceId;
    $('claim-invoice').value = invoiceId;
    $('payment-detail-title').textContent = invoice.description || 'ค่าเรียน';
    $('payment-detail-invoice').textContent = `ใบแจ้งยอด ${invoice.invoice_number} · ครบกำหนด ${thaiDate(invoice.due_date)}`;
    $('claim-display-amount').textContent = thaiMoney(invoice.amount_satang - invoice.paid_satang);
    $('payment-list').hidden = true;
    $('payment-detail').hidden = false;
  }

  function renderStudentClassSchedule() {
    const classBody = $('student-classes-rows');
    const classAlerts = $('student-class-alerts');
    if (!classBody || !classAlerts) return;
    classBody.replaceChildren();
    classAlerts.replaceChildren();
    const nowEpoch = Date.now() / 1000;
    const recentCancellations = state.classes.filter((item) => item.status === 'cancelled'
      && (Number(item.cancelled_at || 0) >= nowEpoch - 30 * 86400 || epochValue(item.ends_at) >= nowEpoch - 7 * 86400))
      .sort((a, b) => Number(b.cancelled_at || 0) - Number(a.cancelled_at || 0));
    for (const item of recentCancellations) {
      const alert = element('article', 'class-cancel-alert');
      alert.append(element('strong', '', `ยกคลาส · ${item.course_title}`));
      alert.append(element('p', '', `คาบเดิม ${classSummary(item)} · ${item.room_name || 'ยังไม่ระบุห้อง'}`));
      alert.append(element('p', 'class-cancel-reason', `เหตุผล: ${cancellationReason(item) || 'โปรดติดต่อผู้ดูแลเพื่อสอบถามรายละเอียด'}`));
      classAlerts.append(alert);
    }
    if (!state.classes.length) emptyRow(classBody, 5, state.loadingClasses
      ? 'กำลังโหลดตารางเรียน…' : 'ยังไม่ได้รับการจัดเข้าห้องเรียน ผู้ดูแลจะกำหนดห้องและเวลาให้');
    for (const item of state.classes) {
      const row = element('tr');
      if (item.status === 'cancelled') row.classList.add('is-cancelled');
      const courseCell = cell(row, item.course_title);
      courseCell.prepend(element('strong', 'record-code', `${item.course_code || '—'} · `));
      cell(row, item.room_name || '—');
      cell(row, classSummary(item));
      cell(row, `${item.teacher_name || '—'}${item.teacher_code ? ` · ${item.teacher_code}` : ''}`);
      const classStatus = cell(row, item.status === 'cancelled' ? 'ยกคลาสแล้ว' : status(item.status), item.status === 'cancelled' ? 'cancelled-status' : '');
      if (item.status === 'cancelled' && cancellationReason(item)) classStatus.append(element('div', 'class-cancel-reason', `เหตุผล: ${cancellationReason(item)}`));
      classBody.append(row);
    }
  }

  function showStudentCancellationAlert(cancelled) {
    const region = $('student-live-cancellation');
    if (!region || !cancelled.length) return;
    region.replaceChildren();
    const details = element('div');
    details.append(element('strong', '', cancelled.length === 1 ? 'แจ้งยกคลาสเรียน' : `แจ้งยกคลาสเรียน ${cancelled.length} คาบ`));
    for (const item of cancelled.slice(0, 3)) {
      details.append(element('p', '', `${item.course_title} · ${thaiDateTime(epochValue(item.starts_at))} · เหตุผล: ${cancellationReason(item) || 'โปรดติดต่อผู้ดูแลเพื่อสอบถามรายละเอียด'}`));
    }
    if (cancelled.length > 3) details.append(element('p', '', `และอีก ${cancelled.length - 3} คาบ ดูรายละเอียดในตารางเรียน`));
    const actions = element('div', 'class-live-actions');
    const view = actionButton('ดูตารางเรียน', 'jump'); view.dataset.target = 'student-classes';
    actions.append(view, actionButton('ปิดประกาศ', 'dismiss-class-alert'));
    region.append(details, actions);
    region.hidden = false;
  }

  async function pollStudentClasses() {
    if (state.me?.role !== 'student' || document.hidden || studentClassPollBusy || $('live-dialog')?.open) return;
    const table = $('student-classes-rows');
    if (!table || Date.now() - lastStudentClassPoll < 5000) return;
    studentClassPollBusy = true;
    lastStudentClassPoll = Date.now();
    try {
      const result = await api('/api/student/classes');
      if (table !== $('student-classes-rows') || $('live-dialog')?.open || !Array.isArray(result.classes)) return;
      if (JSON.stringify(result.classes) === JSON.stringify(state.classes)) return;
      const previous = new Map(state.classes.map((item) => [item.id, item.status]));
      const newlyCancelled = result.classes.filter((item) => item.status === 'cancelled' && previous.get(item.id) !== 'cancelled');
      state.classes = result.classes;
      renderStudentClassSchedule();
      showStudentCancellationAlert(newlyCancelled);
    } catch (_error) {
      // The next visible-tab check retries; leave the current timetable intact.
    } finally {
      studentClassPollBusy = false;
    }
  }

  function startStudentClassPolling() {
    if (studentClassPollingStarted) return;
    studentClassPollingStarted = true;
    lastStudentClassPoll = Date.now();
    window.setInterval(() => { void pollStudentClasses(); }, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void pollStudentClasses(); });
    window.addEventListener('focus', () => { void pollStudentClasses(); });
    $('live-dialog').addEventListener('close', () => { void pollStudentClasses(); });
  }

  function renderTeacher() {
    const sessions = state.data.sessions || [];
    const payroll = state.data.payroll_runs || [];
    $('teacher-session-count').textContent = String(sessions.filter((item) => item.status === 'scheduled').length);
    $('teacher-pending-count').textContent = String(sessions.filter((item) => item.time_status === 'submitted').length);
    $('teacher-paid-total').textContent = state.loadingDetails ? '—' :
      thaiMoney(payroll.filter((run) => run.status === 'paid').reduce((sum, run) => sum + run.total_satang, 0));
    const rejected = sessions.filter((item) => item.time_status === 'rejected');
    const nextSession = sessions.filter((item) => item.status === 'scheduled' && Number(item.starts_at) > Date.now() / 1000)
      .sort((a, b) => a.starts_at - b.starts_at)[0];
    let focus = null;
    if (rejected.length) focus = focusPanel('มีเวลาสอนที่ต้องส่งใหม่', `${rejected.length} คาบถูกส่งกลับ โปรดตรวจคาบและลงเวลาสอนอีกครั้ง`, 'ดูคาบสอน', 'overview');
    else if (nextSession) {
      const nextClass = state.classes.find((item) => item.id === nextSession.id);
      focus = focusPanel('คาบสอนถัดไป', `${nextSession.title} · ${nextClass?.room_name || (state.loadingClasses ? 'กำลังโหลดห้อง' : 'รอแจ้งห้อง')} · ${thaiDateTime(nextSession.starts_at)} – ${thaiDateTime(nextSession.ends_at)}`, 'ดูตารางสอน', 'overview');
    }
    if (focus) $('overview').querySelector('.stats').after(focus);
    const body = $('teacher-sessions');
    if (!sessions.length) emptyRow(body, 6, 'ยังไม่มีคาบสอนที่ได้รับมอบหมาย');
    const nowEpoch = Date.now() / 1000;
    const orderedSessions = [...sessions].sort((a, b) => {
      const aUpcoming = a.status === 'scheduled' && Number(a.starts_at) >= nowEpoch;
      const bUpcoming = b.status === 'scheduled' && Number(b.starts_at) >= nowEpoch;
      if (aUpcoming !== bUpcoming) return aUpcoming ? -1 : 1;
      return aUpcoming ? a.starts_at - b.starts_at : b.starts_at - a.starts_at;
    });
    for (const item of orderedSessions) {
      const classroom = state.classes.find((entry) => entry.id === item.id);
      const row = element('tr');
      if (item.status === 'scheduled' && Number(item.starts_at) >= nowEpoch) row.classList.add('is-upcoming');
      const title = cell(row, item.title);
      title.append(element('div', 'muted', `${classroom?.course_code || ''} ${subjectLabel(item.subject)}`.trim()));
      const location = cell(row, classroom?.room_name || (state.loadingClasses ? 'กำลังโหลด…' : '—'));
      location.append(element('div', 'muted', `${thaiDateTime(item.starts_at)} – ${thaiDateTime(item.ends_at)}`));
      cell(row, classroom ? `${classroom.roster_count ?? 0} คน` : (state.loadingClasses ? 'กำลังโหลด…' : '0 คน'));
      const sessionStatus = cell(row, item.status === 'cancelled' ? 'ยกคลาสแล้ว' : status(item.status), item.status === 'cancelled' ? 'cancelled-status' : '');
      if (item.status === 'cancelled' && cancellationReason(item)) sessionStatus.append(element('div', 'class-cancel-reason', `เหตุผล: ${cancellationReason(item)}`));
      const timeState = item.status === 'cancelled' ? 'ไม่ต้องลงเวลา'
        : item.time_status === 'rejected' ? 'ส่งกลับ · ลงเวลาใหม่ได้'
        : item.time_status === 'approved' ? `อนุมัติแล้ว · ${thaiMoney(item.amount_satang)}`
          : item.time_status ? status(item.time_status) : 'ยังไม่ลงเวลา';
      const timeCell = cell(row, timeState, 'time-status');
      timeCell.dataset.state = item.time_status || 'none';
      if (item.time_status === 'rejected' && item.rejection_reason) {
        timeCell.append(element('div', 'muted', `เหตุผล: ${item.rejection_reason}`));
      }
      const actions = cell(row, ''); actions.className = 'row-actions';
      if (item.status === 'scheduled') {
        const roster = actionButton('เช็กชื่อ', 'roster', item.id);
        roster.disabled = state.loadingDetails;
        actions.append(roster);
      }
      if ((!item.time_entry_id || item.time_status === 'rejected') && item.status === 'scheduled') {
        const time = actionButton(item.time_status === 'rejected' ? 'ส่งเวลาใหม่' : 'ลงเวลา', 'select-time', item.id, 'primary');
        time.disabled = state.loadingDetails;
        actions.append(time);
      }
      if (Boolean(item.can_cancel)) {
        const cancel = actionButton('ยกคลาส', 'teacher-cancel-class', item.id, 'cancel-class-button');
        cancel.disabled = state.loadingDetails;
        actions.append(cancel);
      }
      body.append(row);
    }
    const select = $('time-session');
    option(select, '', 'เลือกคาบเรียน');
    for (const item of sessions.filter((entry) => entry.status === 'scheduled' && (!entry.time_entry_id || entry.time_status === 'rejected'))) {
      option(select, item.id, `${item.title} · ${thaiDateTime(item.starts_at)}`);
    }
    select.addEventListener('change', () => setTeacherTime(select.value));
    const payBody = $('teacher-payroll-rows');
    $('teacher-approved-unpaid').textContent = `ค่าจ้างที่อนุมัติแล้วและยังไม่เข้ารอบจ่าย: ${thaiMoney(state.data.approved_unpaid_satang || 0)}`;
    if (!payroll.length) emptyRow(payBody, 5, 'ยังไม่มีงวดค่าจ้างที่ปิดยอด');
    for (const run of payroll) {
      const row = element('tr');
      cell(row, run.period); cell(row, thaiMoney(run.total_satang)); cell(row, status(run.status));
      cell(row, run.paid_at ? thaiDateTime(run.paid_at) : 'ยังไม่จ่าย');
      cell(row, '').append(actionButton('ดูรายการ', 'payroll-detail', run.id)); payBody.append(row);
    }
  }

  function setTeacherTime(sessionId) {
    const session = (state.data.sessions || []).find((item) => item.id === sessionId);
    $('time-session').value = sessionId || '';
    $('actual-start').value = session ? bangkokInput(session.starts_at) : '';
    $('actual-end').value = session ? bangkokInput(session.ends_at) : '';
  }

  function fillSubjectOptions() {
    const level = $('course-level').value;
    const select = $('course-subject');
    select.replaceChildren();
    for (const [code, label] of courseSubjects[level] || []) option(select, code, label);
    $('course-price').placeholder = level === 'high' ? 'เช่น 4500' : 'เช่น 3500';
  }

  function eligibleStudents(courseId) {
    const students = new Map();
    for (const enrollment of state.academic?.enrollments || []) {
      if (enrollment.course_id === courseId && enrollment.status === 'active') {
        students.set(enrollment.student_id, enrollment);
      }
    }
    return [...students.values()].sort((a, b) => String(a.student_name).localeCompare(String(b.student_name), 'th'));
  }

  function fillClassStudentPicker(courseId, selectedIds = [], container = $('admin-class-students')) {
    if (!container) return;
    container.replaceChildren();
    if (!courseId) { container.append(element('p', 'muted', 'เลือกคอร์สก่อน แล้วระบบจะแสดงผู้สมัครเรียนที่จัดเข้าห้องได้')); return; }
    const eligible = eligibleStudents(courseId);
    if (!eligible.length) { container.append(element('p', 'muted', 'ยังไม่มีนักเรียนที่สมัครคอร์สนี้')); return; }
    const chosen = new Set(selectedIds);
    for (const student of eligible) {
      const label = element('label', 'class-student-option');
      const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.name = 'student_ids'; checkbox.value = student.student_id;
      checkbox.checked = chosen.has(student.student_id);
      label.append(checkbox, element('span', '', `${student.student_code || '—'} · ${student.student_name}`));
      container.append(label);
    }
  }

  function chosenStudentIds(form) {
    return [...form.querySelectorAll('input[name="student_ids"]:checked')].map((node) => validId(node.value));
  }

  function fillAdminEnrollmentStudents(courseId) {
    const select = $('admin-enroll-student');
    select.replaceChildren(); option(select, '', courseId ? 'เลือกนักเรียน' : 'เลือกคอร์สก่อน');
    const course = (state.academic?.courses || []).find((item) => item.id === courseId);
    if (!course) return;
    for (const person of state.academic?.users || []) {
      if (person.role !== 'student' || person.status !== 'active' || !person.grade) continue;
      const middleGrade = ['M1', 'M2', 'M3'].includes(person.grade);
      if (middleGrade !== (course.level === 'middle')) continue;
      if ((state.academic?.enrollments || []).some((entry) => entry.course_id === courseId && entry.student_id === person.id)) continue;
      option(select, person.id, `${person.student_code || '—'} · ${person.name} · ${person.grade}`);
    }
  }

  async function claimVerificationDialog(claimId) {
    const claim = (state.data?.pending_payment_claims || []).find((item) => item.id === claimId);
    if (!claim) throw new Error('ไม่พบรายการแจ้งชำระที่รอตรวจ');
    const endpoint = `/api/admin/payment-claims/${claimId}/verification`;
    const review = await api(endpoint);
    const precheck = review.precheck || {};
    const verified = review.verification || {};
    const dialog = $('live-dialog');
    dialog.replaceChildren();
    dialog.classList.add('claim-review-dialog');
    const heading = element('h2', '', `ตรวจสลิป · ${claim.student_name}`);
    heading.id = 'claim-review-title';
    dialog.setAttribute('aria-labelledby', heading.id);
    dialog.append(heading, element('p', 'muted', `${claim.invoice_number || 'ใบแจ้งยอด'} · ${thaiMoney(claim.amount_satang)}`));
    const summary = element('div', 'claim-review-summary');
    summary.dataset.state = precheck.status === 'passed' ? 'passed' : 'failed';
    summary.append(element('strong', '', precheck.status === 'passed'
      ? 'ผลตรวจเบื้องต้น: ผ่าน' : 'ผลตรวจเบื้องต้น: ไม่ผ่าน'));
    summary.append(element('p', '', precheck.message || 'ระบบยังไม่สามารถยืนยันข้อมูลสลิปเบื้องต้นได้'));
    summary.append(element('small', '', 'ผลเบื้องต้นไม่ยืนยันว่ามีเงินเข้าบัญชี ผู้ดูแลต้องตรวจรายการเดินบัญชีจริง'));
    dialog.append(summary);
    const qr = precheck.qr || { status: 'unavailable', message: 'ยังไม่มีผลอ่าน QR จากเซิร์ฟเวอร์' };
    const qrLabels = {
      decoded: 'อ่าน QR ได้ — ยังไม่ยืนยันว่ามีการโอนจริง',
      not_found: 'ไม่พบ QR ในสลิป',
      invalid: 'ข้อมูล QR ไม่ถูกต้อง',
      unreadable: 'อ่าน QR ไม่สำเร็จ',
      unavailable: 'ยังตรวจ QR ไม่ได้'
    };
    const qrPanel = element('section', 'claim-qr-result');
    qrPanel.dataset.state = qr.status || 'unavailable';
    qrPanel.append(element('strong', '', qrLabels[qr.status] || qrLabels.unavailable));
    qrPanel.append(element('p', '', qr.message || 'ผล QR ใช้ประกอบการตรวจเท่านั้น'));
    if (qr.status === 'decoded' && qr.payload_type) {
      const typeLabel = { emv_payment: 'รหัสชำระเงิน', url: 'ลิงก์', text: 'ข้อความ' }[qr.payload_type] || 'ข้อมูลทั่วไป';
      qrPanel.append(element('small', '', `ประเภทข้อมูล: ${typeLabel}${qr.checksum_valid === true ? ' · checksum ผ่าน' : ''}`));
    }
    qrPanel.append(element('small', '', 'ระบบไม่เปิดลิงก์ใน QR และไม่ใช้ QR แทนการตรวจเงินเข้าบัญชีธนาคาร'));
    dialog.append(qrPanel);
    if (Array.isArray(precheck.checks) && precheck.checks.length) {
      const checks = element('ul', 'claim-review-checks');
      for (const check of precheck.checks) {
        const item = element('li', '', `${check.status === 'passed' ? '✓' : check.status === 'warning' ? '!' : '✕'} ${check.message || check.code || 'ไม่ทราบผล'}`);
        item.dataset.state = check.status === 'passed' ? 'passed' : check.status === 'warning' ? 'warning' : 'failed';
        checks.append(item);
      }
      dialog.append(checks);
    }
    if (verified.status === 'correct' || verified.status === 'incorrect') {
      const history = element('p', 'claim-review-history', `ผลตรวจที่บันทึกไว้: ${verified.status === 'correct' ? 'ถูกต้อง' : 'ไม่ถูกต้อง'}${verified.reviewer_name ? ` · ${verified.reviewer_name}` : ''}`);
      if (verified.note) history.append(element('span', '', `เหตุผล: ${verified.note}`));
      dialog.append(history);
    }
    if (claim.reference?.startsWith('SLIP-')) {
      const preview = element('div', 'claim-slip-preview');
      const image = element('img');
      image.alt = `สลิปที่ ${claim.student_name} แนบสำหรับ ${claim.invoice_number || 'รายการแจ้งชำระ'}`;
      image.hidden = true;
      preview.append(element('p', 'muted', 'กำลังโหลดภาพสลิป…'), image);
      const link = element('a', 'slip-download', 'เปิดไฟล์สลิป');
      link.href = `/api/admin/payment-claims/${claimId}/slip`;
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      preview.append(link); dialog.append(preview);
      const abort = new AbortController();
      let blobUrl = '';
      dialog.addEventListener('close', () => { abort.abort(); if (blobUrl) URL.revokeObjectURL(blobUrl); }, { once: true });
      void fetch(link.href, { credentials: 'same-origin', cache: 'no-store', signal: abort.signal })
        .then(async (response) => {
          if (!response.ok || !['image/jpeg', 'image/png', 'image/webp'].includes(response.headers.get('content-type')?.split(';')[0])) throw new Error('ไม่สามารถแสดงภาพสลิปได้');
          const blob = await response.blob();
          if (!dialog.open || !blob.size) return;
          blobUrl = URL.createObjectURL(blob);
          image.src = blobUrl;
          image.hidden = false;
          preview.querySelector('p').hidden = true;
        }).catch((error) => {
          if (error.name !== 'AbortError' && dialog.open) preview.querySelector('p').textContent = 'ไม่สามารถแสดงภาพได้ กรุณากดเปิดไฟล์สลิป';
        });
    }
    const form = element('form', 'live-form claim-review-form');
    const verdictLabel = element('label', '', 'ผลตรวจของผู้ดูแล');
    const verdictSelect = element('select');
    verdictSelect.name = 'verdict'; verdictSelect.required = true;
    option(verdictSelect, '', 'เลือกผลตรวจ');
    const correctOption = element('option', '', 'ถูกต้อง — ตรวจยอดในบัญชีธนาคารแล้ว');
    correctOption.value = 'correct'; correctOption.disabled = precheck.status !== 'passed';
    verdictSelect.append(correctOption);
    option(verdictSelect, 'incorrect', 'ไม่ถูกต้อง — ระบุเหตุผล');
    verdictLabel.append(verdictSelect); form.append(verdictLabel);
    const bankLabel = element('label', 'claim-bank-confirmation');
    const bankCheckbox = element('input'); bankCheckbox.type = 'checkbox'; bankCheckbox.name = 'bank_confirmed';
    bankLabel.append(bankCheckbox, element('span', '', 'ฉันตรวจรายการเงินเข้าบัญชีธนาคารจริงแล้ว และยอดตรงกับใบแจ้งยอด'));
    bankLabel.hidden = true; form.append(bankLabel);
    const reasonLabel = element('label', '', 'เหตุผลที่ไม่ถูกต้อง');
    const reason = element('textarea'); reason.name = 'note'; reason.maxLength = 250; reason.rows = 3;
    reason.placeholder = 'เช่น ยอดไม่ตรง / พบสลิปซ้ำ / ไม่พบรายการเงินเข้า';
    reasonLabel.append(reason); reasonLabel.hidden = true; form.append(reasonLabel);
    const updateFields = () => {
      const correct = verdictSelect.value === 'correct';
      const incorrect = verdictSelect.value === 'incorrect';
      bankLabel.hidden = !correct; bankCheckbox.required = correct;
      reasonLabel.hidden = !incorrect; reason.required = incorrect;
    };
    verdictSelect.addEventListener('change', updateFields);
    const errorLine = element('p', 'live-modal-feedback'); errorLine.setAttribute('role', 'alert'); form.append(errorLine);
    const actions = element('div', 'actions');
    const cancel = element('button', '', 'ปิด'); cancel.type = 'button';
    const save = element('button', 'primary', 'บันทึกผลตรวจ'); save.type = 'submit';
    actions.append(cancel, save); form.append(actions); dialog.append(form);
    return new Promise((resolve) => {
      let savedReview = null;
      let isSaving = false;
      const preventCloseWhileSaving = (event) => { if (isSaving) event.preventDefault(); };
      dialog.addEventListener('cancel', preventCloseWhileSaving);
      cancel.onclick = () => dialog.close();
      form.onsubmit = async (event) => {
        event.preventDefault();
        const verdict = verdictSelect.value;
        const note = reason.value.trim();
        if (!['correct', 'incorrect'].includes(verdict)) return;
        if (verdict === 'correct' && (!bankCheckbox.checked || precheck.status !== 'passed')) {
          errorLine.textContent = 'กรุณาตรวจยอดเงินเข้าจริงและยืนยันให้ครบก่อนบันทึกว่าถูกต้อง';
          return;
        }
        if (verdict === 'incorrect' && !note) { errorLine.textContent = 'กรุณาระบุเหตุผลที่สลิปไม่ถูกต้อง'; return; }
        isSaving = true; save.disabled = true; cancel.disabled = true; errorLine.textContent = 'กำลังบันทึกผลตรวจ…';
        try {
          savedReview = await api(endpoint, 'POST', { verdict, note, bank_confirmed: verdict === 'correct' && bankCheckbox.checked });
          dialog.close();
        } catch (error) {
          errorLine.textContent = error.message || 'บันทึกผลตรวจไม่สำเร็จ';
          isSaving = false;
          save.disabled = false; cancel.disabled = false;
        }
      };
      dialog.onclose = () => {
        dialog.removeEventListener('cancel', preventCloseWhileSaving);
        dialog.classList.remove('claim-review-dialog');
        dialog.removeAttribute('aria-labelledby');
        dialog.replaceChildren();
        resolve(savedReview);
      };
      dialog.showModal();
    });
  }

  function renderAdmin() {
    const invoices = state.data.invoices || [];
    const unbilled = (state.data.unbilled_enrollments || []).filter((item) =>
      (state.academic?.courses || []).find((course) => course.id === item.course_id)?.course_format !== 'individual');
    const claims = state.data.pending_payment_claims || [];
    const times = state.data.pending_time_entries || [];
    const overview = state.data.overview_counts || null;
    const rates = state.data.rates || [];
    const runs = state.data.payroll_runs || [];
    const users = state.data.users || [];
    const notifications = state.data.notifications || {};
    const notificationCounts = notifications.counts || {};
    const failedNotices = notifications.failed || [];
    const teachers = users.filter((person) => person.role === 'teacher');
    const staffBody = $('admin-staff-rows');
    $('staff-total').textContent = `ครูทั้งหมด ${teachers.length} คน`;
    $('staff-pending').textContent = `รอเปิดบัญชี ${teachers.filter((person) => person.roster_pending).length} คน`;
    if (!teachers.length) emptyRow(staffBody, 4, 'ยังไม่มีครูผู้สอน');
    for (const person of teachers) {
      const row = element('tr');
      cell(row, person.teacher_code || '—', 'record-code');
      const identity = cell(row, person.name);
      if (person.email) identity.append(element('div', 'muted', person.email));
      cell(row, person.roster_pending ? 'รอเปิดบัญชี' : person.status === 'active' ? 'เข้าใช้ระบบได้' : 'ปิดบัญชี');
      const actions = cell(row, ''); actions.className = 'row-actions';
      actions.append(actionButton('แก้ชื่อ', 'edit-user', person.id));
      if (person.roster_pending) actions.append(actionButton('เปิดบัญชีครู', 'staff-activate', person.id, 'primary'));
      else {
        actions.append(actionButton('รีเซ็ตรหัส', 'reset-user', person.id));
        actions.append(actionButton(person.status === 'active' ? 'ปิดบัญชี' : 'เปิดบัญชี', person.status === 'active' ? 'disable-user' : 'enable-user', person.id));
      }
      staffBody.append(row);
    }
    $('admin-staff-search').addEventListener('input', (event) => {
      const query = event.currentTarget.value.trim().toLocaleLowerCase('th');
      for (const row of staffBody.rows) row.hidden = Boolean(query) && !row.textContent.toLocaleLowerCase('th').includes(query);
    });
    const studentUsers = users.filter((person) => person.role === 'student');
    const userBody = $('admin-user-rows');
    if (!studentUsers.length) emptyRow(userBody, 6, 'ยังไม่มีบัญชีนักเรียน');
    for (const person of studentUsers) {
      const row = element('tr');
      cell(row, personalCode(person), 'record-code');
      const identity = cell(row, person.name); identity.append(element('div', 'muted', person.email || person.username || '—'));
      cell(row, roleNames[person.role] || person.role); cell(row, person.status === 'active' ? 'ใช้งาน' : 'ปิดใช้งาน');
      cell(row, person.must_change_password ? 'ต้องเปลี่ยนเมื่อเข้าสู่ระบบ' : 'เปลี่ยนแล้ว');
      const actions = cell(row, ''); actions.className = 'row-actions';
      actions.append(actionButton('ข้อมูลนักเรียน', 'admin-profile', person.id));
      actions.append(actionButton('แก้ชื่อ/อีเมล', 'edit-user', person.id));
      actions.append(actionButton('รีเซ็ตรหัส', 'reset-user', person.id));
      actions.append(actionButton(person.status === 'active' ? 'ปิดบัญชี' : 'เปิดบัญชี', person.status === 'active' ? 'disable-user' : 'enable-user', person.id));
      userBody.append(row);
    }
    $('admin-user-search').addEventListener('input', (event) => {
      const query = event.currentTarget.value.trim().toLocaleLowerCase('th');
      for (const row of userBody.rows) row.hidden = Boolean(query) && !row.textContent.toLocaleLowerCase('th').includes(query);
    });
    $('admin-outstanding').textContent = thaiMoney(overview ? overview.outstanding_satang : invoices.filter((item) => item.status !== 'void').reduce((sum, item) => sum + Math.max(0, item.amount_satang - item.paid_satang), 0));
    $('admin-outstanding').nextElementSibling.textContent = overview
      ? `จากใบแจ้งยอดทั้งหมด ${overview.invoice_count} รายการ`
      : `จาก ${invoices.length} ใบแจ้งยอดล่าสุดที่แสดง`;
    $('admin-claim-count').textContent = String(overview ? overview.pending_claims : claims.length);
    $('admin-time-count').textContent = String(overview ? overview.pending_times : times.length);
    const overdue = invoices.filter((item) => item.status !== 'void' && item.due_date < bangkokToday() && item.amount_satang > item.paid_satang);
    const queue = element('section', 'panel action-queue');
    queue.append(element('h2', '', 'งานที่ต้องจัดการ'));
    queue.append(element('p', 'muted', overview
      ? 'สรุปจากข้อมูลทั้งหมดในฐานข้อมูล กดเพื่อไปยังรายการที่เกี่ยวข้อง'
      : 'จำนวนแสดงจากข้อมูลล่าสุดที่โหลดในหน้านี้ กดเพื่อไปยังรายการที่เกี่ยวข้อง'));
    const queueGrid = element('div', 'queue-grid');
    queueGrid.append(
      queueItem(overview ? overview.unbilled_count : unbilled.length, 'คอร์สที่ยังไม่มีใบแจ้งยอด', 'ตรวจรายการที่เพิ่มด้วยมือหรือย้ายข้อมูล', 'admin-finance', true),
      queueItem(overview ? overview.pending_claims : claims.length, 'รอตรวจการชำระ', 'ตรวจเงินและเลขอ้างอิงก่อนกดยืนยัน', 'admin-claims', true),
      queueItem(overview ? overview.pending_times : times.length, 'รอตรวจเวลาสอน', 'เช็กชื่อและตรวจอัตราก่อนอนุมัติ', 'admin-teaching', true),
      queueItem(overview ? overview.overdue_count : overdue.length, 'ใบแจ้งยอดเลยกำหนด', 'ยังมียอดคงเหลือที่ต้องติดตาม', 'admin-invoices', true),
      queueItem(overview ? overview.failed_notices : Number(notificationCounts.dead) || 0, 'อีเมลส่งไม่สำเร็จ', 'ตรวจสถานะการแจ้งเตือน', 'admin-notifications', true)
    );
    queue.append(queueGrid);
    $('overview').querySelector('.stats').after(queue);
    const workload = workloadPanel([
      ['คอร์สที่ยังไม่มีใบแจ้งยอด', overview ? overview.unbilled_count : unbilled.length],
      ['รอตรวจการชำระ', overview ? overview.pending_claims : claims.length],
      ['รอตรวจเวลาสอน', overview ? overview.pending_times : times.length],
      ['ใบแจ้งยอดเลยกำหนด', overview ? overview.overdue_count : overdue.length],
      ['อีเมลส่งไม่สำเร็จ', overview ? overview.failed_notices : Number(notificationCounts.dead) || 0]
    ]);
    queue.after(workload);
    const notificationPanel = element('section', 'panel');
    notificationPanel.id = 'admin-notifications';
    notificationPanel.append(element('h2', '', 'สถานะอีเมลแจ้งเตือน'));
    notificationPanel.append(element('p', 'muted', `รอส่ง ${Number(notificationCounts.pending || 0) + Number(notificationCounts.sending || 0)} · ส่งแล้ว ${Number(notificationCounts.sent) || 0} · ส่งไม่สำเร็จ ${Number(notificationCounts.dead) || 0}`));
    if (failedNotices.length) {
      const wrap = element('div', 'tablewrap');
      const table = element('table');
      const head = element('thead');
      const headRow = element('tr');
      for (const label of ['เวลาที่เกิด', 'เหตุการณ์', 'จำนวนครั้ง', 'สาเหตุ']) headRow.append(element('th', '', label));
      head.append(headRow);
      const body = element('tbody');
      for (const notice of failedNotices) {
        const row = element('tr');
        cell(row, thaiDateTime(notice.created_at));
        cell(row, notice.event_type || 'ข้อมูลอัปเดต');
        cell(row, String(notice.attempts ?? '—'));
        cell(row, notice.last_error_code || 'ไม่ทราบสาเหตุ');
        body.append(row);
      }
      table.append(head, body); wrap.append(table); notificationPanel.append(wrap);
    } else notificationPanel.append(element('p', 'muted', 'ยังไม่มีอีเมลส่งไม่สำเร็จ'));
    workload.after(notificationPanel);
    if (overview) notificationPanel.hidden = true;
    const enrollmentSelect = $('invoice-enrollment'); option(enrollmentSelect, '', 'เลือกนักเรียนและคอร์ส');
    for (const enrollment of unbilled) option(enrollmentSelect, enrollment.enrollment_id, `${enrollment.student_name} · ${enrollment.course_title}`);
    enrollmentSelect.addEventListener('change', () => {
      const enrollment = unbilled.find((item) => item.enrollment_id === enrollmentSelect.value);
      const course = (state.academic?.courses || []).find((item) => item.id === enrollment?.course_id);
      $('invoice-amount').value = course ? (Number(course.price_satang) / 100).toFixed(2) : '';
    });
    const invoiceBody = $('admin-invoices');
    if (!invoices.length) emptyRow(invoiceBody, 6, 'ยังไม่มีใบแจ้งยอด');
    const orderedInvoices = [...invoices].sort((a, b) => {
      const aOverdue = overdue.includes(a) ? 1 : 0;
      const bOverdue = overdue.includes(b) ? 1 : 0;
      return bOverdue - aOverdue || String(b.created_at || '').localeCompare(String(a.created_at || ''));
    });
    for (const invoice of orderedInvoices) {
      const row = element('tr'); const title = cell(row, invoice.invoice_number); title.append(element('div', 'muted', invoice.student_name));
      cell(row, thaiMoney(invoice.amount_satang)); cell(row, thaiMoney(Math.max(0, invoice.amount_satang - invoice.paid_satang)));
      cell(row, thaiDate(invoice.due_date));
      const isOverdue = overdue.includes(invoice);
      const invoiceState = cell(row, isOverdue ? 'เกินกำหนด' : status(invoice.status), 'invoice-status');
      if (isOverdue) { row.classList.add('is-overdue'); invoiceState.dataset.state = 'overdue'; }
      const actions = cell(row, ''); actions.className = 'row-actions';
      if (!invoice.individual_period && invoice.status === 'unpaid' && !claims.some((claim) => claim.invoice_id === invoice.id)) actions.append(actionButton('แก้ยอด', 'edit-invoice-amount', invoice.id));
      if (invoice.status === 'unpaid' || invoice.status === 'partial') actions.append(actionButton('เปลี่ยนกำหนด', 'edit-invoice-due-date', invoice.id));
      if (!invoice.individual_period && invoice.status === 'unpaid' && !invoice.paid_satang && !claims.some((claim) => claim.invoice_id === invoice.id)) actions.append(actionButton('ยกเลิกใบแจ้งยอด', 'void-invoice', invoice.id));
      if (!invoice.individual_period && invoice.status === 'void') actions.append(actionButton('คืนสถานะใบแจ้งยอด', 'restore-invoice', invoice.id));
      invoiceBody.append(row);
    }
    const claimBody = $('admin-claims-rows');
    if (!claims.length) emptyRow(claimBody, 4, 'ไม่มีรายการรอตรวจ');
    for (const claim of claims) {
      const row = element('tr');
      const person = cell(row, `${claim.student_code || '—'} · ${claim.student_name}`);
      person.append(element('div', 'muted', `${claim.invoice_number || 'ใบแจ้งยอด'} · ${claim.course_title || 'ค่าเรียน'}`));
      person.append(element('div', 'muted', claim.reference.startsWith('SLIP-') ? 'มีสลิปแนบ · กรุณาตรวจยอดจริง' : claim.reference));
      cell(row, thaiMoney(claim.amount_satang));
      const reviewCell = cell(row, '', 'claim-review-cell');
      const precheck = claim.precheck_status || 'unknown';
      const verdict = claim.verification_status || 'needs_review';
      const precheckText = precheck === 'passed' ? 'ข้อมูลสลิปเบื้องต้น: ผ่าน'
        : precheck === 'failed' ? 'ข้อมูลสลิปเบื้องต้น: ไม่ผ่าน' : 'ยังไม่ทราบผลตรวจเบื้องต้น';
      const verdictText = verdict === 'correct' ? 'ผลตรวจผู้ดูแล: ถูกต้อง'
        : verdict === 'incorrect' ? 'ผลตรวจผู้ดูแล: ไม่ถูกต้อง' : 'รอตรวจยอดในบัญชีธนาคาร';
      const precheckBadge = element('span', 'claim-review-badge', precheckText);
      precheckBadge.dataset.state = precheck;
      const verdictBadge = element('span', 'claim-review-badge', verdictText);
      verdictBadge.dataset.state = verdict;
      reviewCell.append(precheckBadge, verdictBadge);
      if (precheck === 'passed' && verdict !== 'correct') reviewCell.append(element('small', 'claim-review-hint', 'ผลเบื้องต้นยังไม่ยืนยันว่ารับเงินจริง'));
      const actions = cell(row, ''); actions.className = 'row-actions claim-row-actions';
      if (claim.reference.startsWith('SLIP-')) {
        const slip = element('a', 'slip-download', 'ดูสลิป');
        slip.href = `/api/admin/payment-claims/${validId(claim.id)}/slip`;
        slip.target = '_blank'; slip.rel = 'noopener noreferrer';
        actions.append(slip);
      }
      actions.append(actionButton('ตรวจสลิป', 'verify-claim', claim.id));
      const approve = actionButton('ยืนยันรับเงิน', 'approve-claim', claim.id, 'primary');
      approve.disabled = !(precheck === 'passed' && verdict === 'correct');
      approve.title = approve.disabled ? 'ต้องตรวจสลิปและยืนยันยอดในบัญชีธนาคารก่อน' : 'ยืนยันรับเงินและออกใบสำคัญรับเงิน';
      const reject = actionButton('ปฏิเสธ', 'reject-claim', claim.id);
      reject.disabled = verdict !== 'incorrect';
      reject.title = reject.disabled ? 'ต้องบันทึกผลตรวจว่าไม่ถูกต้องพร้อมเหตุผลก่อน' : 'ปฏิเสธและแจ้งนักเรียน';
      actions.append(approve, reject); claimBody.append(row);
    }
    const invoiceSelect = $('payment-invoice'); option(invoiceSelect, '', 'เลือกใบแจ้งยอด');
    for (const invoice of invoices.filter((item) => item.status !== 'void' && item.amount_satang > item.paid_satang)) {
      option(invoiceSelect, invoice.id, `${invoice.invoice_number} · ${invoice.student_name} · คงเหลือ ${thaiMoney(invoice.amount_satang - invoice.paid_satang)}`);
    }
    invoiceSelect.addEventListener('change', () => {
      const invoice = invoices.find((item) => item.id === invoiceSelect.value);
      $('payment-amount').value = invoice ? ((invoice.amount_satang - invoice.paid_satang) / 100).toFixed(2) : '';
    });
    const receiptPanel = element('section', 'panel receipt-panel');
    receiptPanel.append(element('h2', '', 'ใบสำคัญรับเงินที่ออกแล้ว'));
    receiptPanel.append(element('p', 'muted', 'แสดง 200 รายการล่าสุด ออกตามใบแจ้งยอดและยอดที่ตรวจรับจริง'));
    const receiptWrap = element('div', 'tablewrap');
    const receiptTable = element('table');
    const receiptHead = element('thead');
    const receiptHeadRow = element('tr');
    for (const label of ['เลขที่', 'นักเรียน / คอร์ส', 'วันที่รับเงิน', 'ยอดรับจริง', 'เอกสาร']) receiptHeadRow.append(element('th', '', label));
    receiptHead.append(receiptHeadRow);
    const receiptRows = element('tbody');
    const issued = state.data.receipts || [];
    if (!issued.length) emptyRow(receiptRows, 5, 'ยังไม่มีใบสำคัญรับเงิน');
    for (const receipt of issued) {
      const row = element('tr');
      cell(row, receipt.receipt_number, 'record-code');
      const purchase = cell(row, receipt.student_name);
      purchase.append(element('div', 'muted', `${receipt.course_code ? `${receipt.course_code} · ` : ''}${receipt.course_title || receipt.description || 'ค่าเรียน'}`));
      cell(row, thaiDateTime(receipt.paid_at));
      cell(row, thaiMoney(receipt.amount_satang));
      const link = element('a', 'receipt-download', 'ดาวน์โหลด PDF');
      link.href = `/api/receipts/${validId(receipt.id)}.pdf`;
      link.download = `${receipt.receipt_number}.pdf`;
      link.setAttribute('aria-label', `ดาวน์โหลดใบสำคัญรับเงิน ${receipt.receipt_number}`);
      cell(row, '').append(link);
      receiptRows.append(row);
    }
    receiptTable.append(receiptHead, receiptRows);
    receiptWrap.append(receiptTable);
    receiptPanel.append(receiptWrap);
    $('admin-finance').append(receiptPanel);
    const timeBody = $('admin-times');
    if (!times.length) emptyRow(timeBody, 5, 'ไม่มีเวลาสอนรอตรวจ');
    for (const item of times) {
      const row = element('tr'); const title = cell(row, item.teacher_name); title.append(element('div', 'muted', item.course_title));
      cell(row, `${thaiDateTime(item.actual_start)} – ${thaiDateTime(item.actual_end)}`); cell(row, `${item.break_minutes} นาที`);
      cell(row, `${item.student_count} คน`); const actions = cell(row, ''); actions.className = 'row-actions';
      actions.append(actionButton('เช็กชื่อ', 'roster', item.session_id), actionButton('อนุมัติ', 'approve-time', item.id, 'primary'), actionButton('อนุมัติยอดพิเศษ', 'override-time', item.id), actionButton('ปฏิเสธ', 'reject-time', item.id)); timeBody.append(row);
    }
    const rateBody = $('admin-rates-rows');
    if (!rates.length) emptyRow(rateBody, 6, 'ยังไม่มีอัตราค่าจ้าง');
    for (const rule of rates) {
      const row = element('tr');
      const scope = rule.scope === 'global' || !rule.teacher_id ? 'อัตรากลาง' : (rule.teacher_label || 'เฉพาะครู');
      const subject = rule.subject === 'all' ? 'ทุกวิชา' : rule.subject;
      cell(row, `${scope} · ${subject} · ${rule.level === 'high' ? 'มัธยมปลาย' : 'มัธยมต้น'}`);
      cell(row, `${rule.min_students}–${rule.max_students} คน`); cell(row, thaiMoney(rule.rate_satang_per_hour));
      cell(row, status(rule.state)); cell(row, rule.source_note, 'rate-note');
      const actions = cell(row, ''); actions.className = 'row-actions';
      if (rule.state === 'pending_confirmation') actions.append(actionButton('ตรวจและยืนยัน', 'confirm-rate', rule.id, 'primary'));
      if (rule.state === 'confirmed' && !rule.effective_to) actions.append(actionButton('กำหนดวันสิ้นสุด', 'end-rate', rule.id));
      rateBody.append(row);
    }
    const assignableTeachers = teachers.filter((person) => person.status === 'active' || person.roster_pending);
    for (const id of ['course-teacher', 'class-teacher', 'payroll-teacher', 'rate-teacher']) {
      const select = $(id); option(select, '', id === 'course-teacher' ? 'ยังไม่กำหนด — จัดครูภายหลัง' : 'เลือกครู');
      for (const teacher of assignableTeachers) option(select, teacher.id, `${teacher.teacher_code || '—'} · ${teacher.name}${teacher.roster_pending ? ' · รอเปิดบัญชี' : ''}`);
    }
    const rateScope = $('rate-scope');
    const syncRateScope = () => {
      const teacherOnly = rateScope.value === 'teacher';
      $('rate-teacher-field').hidden = !teacherOnly;
      $('rate-teacher').required = teacherOnly;
      if (!teacherOnly) $('rate-teacher').value = '';
      $('rate-subject-field').hidden = !teacherOnly;
      if (!teacherOnly) $('rate-subject').value = 'all';
    };
    rateScope.addEventListener('change', syncRateScope);
    syncRateScope();
    fillSubjectOptions();
    $('course-level').addEventListener('change', fillSubjectOptions);
    const classRoom = $('class-room'); option(classRoom, '', 'เลือกห้องเรียน');
    for (const room of state.academic?.rooms || []) option(classRoom, room, room);
    const classCourse = $('class-course'); option(classCourse, '', 'เลือกคอร์ส');
    for (const course of state.academic?.courses || []) {
      if (course.active) option(classCourse, course.id, `${course.course_code || '—'} · ${course.title} · ${course.course_format === 'individual' ? 'เรียนเดี่ยว 1:1' : 'เรียนกลุ่ม'}`);
    }
    classCourse.addEventListener('change', () => {
      const course = (state.academic?.courses || []).find((item) => item.id === classCourse.value);
      $('class-teacher').value = course?.teacher_id || '';
      fillClassStudentPicker(classCourse.value);
    });
    fillClassStudentPicker('');
    const courseBody = $('admin-course-rows');
    const academicCourses = state.academic?.courses || [];
    if (!academicCourses.length) emptyRow(courseBody, 7, 'ยังไม่มีคอร์สเรียน');
    for (const course of academicCourses) {
      const row = element('tr');
      cell(row, course.course_code || '—', 'record-code');
      const subject = cell(row, `${subjectLabel(course.subject)} · ${levelNames[course.level] || '—'}`);
      const format = element('span', 'course-format-tag', course.course_format === 'individual' ? 'เรียนเดี่ยว 1:1' : 'เรียนกลุ่ม');
      format.dataset.format = course.course_format === 'individual' ? 'individual' : 'group';
      subject.append(element('br'), format);
      cell(row, course.course_format === 'individual' ? `${thaiMoney(INDIVIDUAL_RATE_SATANG_PER_HOUR)} / ชั่วโมง` : thaiMoney(course.price_satang)); cell(row, course.teacher_name || 'รอผู้ดูแลจัดครู', course.teacher_name ? '' : 'awaiting-teacher');
      cell(row, `${course.enrolled_count ?? 0}/${course.capacity}`);
      cell(row, course.active ? 'เปิดรับ' : 'ปิดรับ');
      cell(row, '').append(actionButton(course.teacher_id ? 'แก้ไขคอร์ส' : 'กำหนดครู / แก้ไข', 'course-edit', course.id));
      courseBody.append(row);
    }
    const classBody = $('admin-classes-rows');
    if (!state.classes.length) emptyRow(classBody, 7, 'ยังไม่มีห้องเรียน ผู้ดูแลสามารถสร้างและเลือกนักเรียนเองได้');
    for (const classroom of state.classes) {
      const row = element('tr');
      const subject = cell(row, classroom.course_title);
      subject.prepend(element('strong', 'record-code', `${classroom.course_code || '—'} · `));
      cell(row, classroom.room_name || '—');
      cell(row, classSummary(classroom));
      cell(row, `${classroom.teacher_code || '—'} · ${classroom.teacher_name || '—'}`);
      const roster = cell(row, `${classroom.roster_count ?? classroom.students?.length ?? 0} คน`);
      const names = (classroom.students || []).slice(0, 3).map((person) => person.name).join(', ');
      if (names) roster.append(element('div', 'muted', `${names}${(classroom.students || []).length > 3 ? '…' : ''}`));
      const classStatus = cell(row, classroom.status === 'cancelled' ? 'ยกคลาสแล้ว' : status(classroom.status), classroom.status === 'cancelled' ? 'cancelled-status' : '');
      if (classroom.status === 'cancelled' && cancellationReason(classroom)) classStatus.append(element('div', 'class-cancel-reason', `เหตุผล: ${cancellationReason(classroom)}`));
      const actions = cell(row, ''); actions.className = 'row-actions';
      actions.append(actionButton('ดูห้อง', 'class-detail', classroom.id));
      if (classroom.status === 'scheduled') actions.append(actionButton('แก้ไข', 'class-edit', classroom.id, 'primary'), actionButton('ยกเลิก', 'class-cancel', classroom.id));
      classBody.append(row);
    }
    const enrollCourse = $('admin-enroll-course'); option(enrollCourse, '', 'เลือกคอร์ส');
    for (const course of academicCourses.filter((item) => item.active)) option(enrollCourse, course.id, `${course.course_code || '—'} · ${course.title} · ${course.course_format === 'individual' ? 'เรียนเดี่ยว 1:1' : 'เรียนกลุ่ม'}`);
    enrollCourse.addEventListener('change', () => fillAdminEnrollmentStudents(enrollCourse.value));
    fillAdminEnrollmentStudents('');
    const enrollmentBody = $('admin-enrollment-rows');
    const allEnrollments = state.academic?.enrollments || [];
    if (!allEnrollments.length) emptyRow(enrollmentBody, 5, 'ยังไม่มีการสมัครเรียน');
    for (const enrollment of allEnrollments) {
      const row = element('tr');
      cell(row, `${enrollment.student_code || '—'} · ${enrollment.student_name}`);
      cell(row, `${enrollment.course_code || '—'} · ${enrollment.course_title}`);
      cell(row, enrollment.status === 'active' ? 'กำลังเรียน' : 'ยกเลิก');
      cell(row, thaiDateTime(enrollment.created_at));
      const actions = cell(row, ''); actions.className = 'row-actions';
      actions.append(actionButton(enrollment.status === 'active' ? 'ยกเลิกการสมัคร' : 'คืนสถานะ', enrollment.status === 'active' ? 'enrollment-cancel' : 'enrollment-restore', enrollment.id));
      enrollmentBody.append(row);
    }
    const payBody = $('admin-payroll-rows');
    if (!runs.length) emptyRow(payBody, 5, 'ยังไม่มีงวดค่าจ้าง');
    for (const run of runs) {
      const row = element('tr'); cell(row, `${run.teacher_name} · ${run.period}`); cell(row, thaiMoney(run.total_satang));
      cell(row, status(run.status)); cell(row, run.paid_at ? thaiDateTime(run.paid_at) : '—');
      const actions = cell(row, ''); actions.className = 'row-actions';
      actions.append(actionButton('ดูรายการ', 'payroll-detail', run.id));
      if (run.status === 'draft') actions.append(actionButton('ปรับยอด', 'adjust-payroll', run.id), actionButton('ปิดงวด', 'finalize-payroll', run.id, 'primary'), actionButton('ยกเลิกร่าง', 'cancel-payroll', run.id));
      if (run.status === 'finalized') actions.append(actionButton('บันทึกจ่าย', 'pay-payroll', run.id, 'primary'));
      payBody.append(row);
    }
  }

  async function delegatedSubmit(event) {
    const form = event.target.closest('form[data-action]');
    if (!form) return;
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    const submit = form.querySelector('[type="submit"]');
    await task(submit, async () => {
      let message = 'บันทึกข้อมูลแล้ว';
      switch (form.dataset.action) {
        case 'student-claim':
          await api('/api/student/payment-claims', 'POST', { invoice_id: validId(values.invoice_id), ...await encodePaymentSlip(values.slip) });
          form.reset();
          message = 'ส่งสลิปแล้ว รอผู้ดูแลตรวจสอบยอดก่อนออกใบสำคัญรับเงิน'; break;
        case 'teacher-time':
          await api('/api/teacher/time-entries', 'POST', { session_id: validId(values.session_id), actual_start: bangkokISO(values.actual_start), actual_end: bangkokISO(values.actual_end), break_minutes: Number(values.break_minutes) });
          message = 'ส่งเวลาสอนให้ผู้ดูแลตรวจแล้ว'; break;
        case 'admin-user-create': {
          const created = await api('/api/admin/users', 'POST', { name: values.name.trim(), email: values.email.trim(), role: values.role });
          await load(); showCredential(created); message = 'สร้างบัญชีแล้ว'; break;
        }
        case 'admin-course':
          await api('/api/admin/courses', 'POST', { subject: values.subject, level: values.level, course_format: values.course_format, target_sessions: Number(values.target_sessions), price_satang: values.course_format === 'individual' ? INDIVIDUAL_RATE_SATANG_PER_HOUR : toSatang(values.price), teacher_id: values.teacher_id ? validId(values.teacher_id) : null, capacity: Number(values.capacity) });
          message = 'สร้างคอร์สและรหัสวิชาแล้ว'; break;
        case 'admin-availability':
          await api('/api/admin/teacher-availability', 'POST', { teacher_id: validId(values.teacher_id), starts_at: bangkokISO(values.starts_at), ends_at: bangkokISO(values.ends_at) });
          message = 'ประกาศช่วงเวลาว่างของครูแล้ว'; break;
        case 'admin-task':
          await api('/api/admin/tasks', 'POST', {
            assignee_id: validId(values.assignee_id), course_id: values.course_id ? validId(values.course_id) : null,
            title: values.title.trim(), description: values.description.trim(), due_date: values.due_date || null
          });
          message = 'มอบหมายงานและแจ้งเตือนบัญชีผู้รับแล้ว'; break;
        case 'admin-class-create':
          await api('/api/admin/classes', 'POST', {
            room_name: values.room_name.trim(), course_id: validId(values.course_id), teacher_id: validId(values.teacher_id),
            starts_at: bangkokISO(values.starts_at), ends_at: bangkokISO(values.ends_at),
            student_ids: chosenStudentIds(form)
          });
          message = 'จัดห้องเรียนและรายชื่อนักเรียนแล้ว'; break;
        case 'admin-enrollment-create':
          await api('/api/admin/enrollments', 'POST', { course_id: validId(values.course_id), student_id: validId(values.student_id) });
          message = 'เพิ่มนักเรียนในคอร์สแล้ว กรุณาจัดเข้าห้องและแจ้งยอดตามขั้นตอน'; break;
        case 'admin-invoice':
          await api('/api/admin/invoices', 'POST', { enrollment_id: validId(values.enrollment_id), amount_satang: toSatang(values.amount), due_date: values.due_date, description: values.description.trim() });
          message = 'แจ้งยอดค่าเรียนในบัญชีนักเรียนแล้ว'; break;
        case 'admin-payment-account':
          await api('/api/admin/payment-account', 'PUT', {
            bank_code: values.bank_code.trim(), bank_name: values.bank_name.trim(),
            account_name: values.account_name.trim(), account_number: values.account_number.trim()
          });
          message = 'บันทึกบัญชีรับโอนแล้ว'; break;
        case 'admin-rate-create':
          await api('/api/admin/rates', 'POST', {
            scope: values.scope,
            ...(values.scope === 'teacher' ? { teacher_id: validId(values.teacher_id) } : {}),
            subject: values.scope === 'global' ? 'all' : values.subject, level: values.level,
            min_students: Number(values.min_students), max_students: Number(values.max_students),
            rate_satang_per_hour: toSatang(values.rate), effective_from: values.effective_from,
            effective_to: values.effective_to || null, source_note: values.source_note.trim()
          });
          message = 'บันทึกอัตราค่าจ้างที่ยืนยันแล้ว'; break;
        case 'admin-payment':
          await api('/api/admin/payments', 'POST', { invoice_id: validId(values.invoice_id), amount_satang: toSatang(values.amount), method: values.method.trim(), reference: values.reference.trim() });
          message = 'บันทึกการรับเงินแล้ว'; break;
        case 'admin-payroll-create':
          await api('/api/admin/payroll-runs', 'POST', { teacher_id: validId(values.teacher_id), period: values.period });
          message = 'สร้างงวดค่าจ้างร่างแล้ว'; break;
        default: throw new Error('ยังไม่รองรับการดำเนินการนี้');
      }
      if (form.dataset.action !== 'admin-user-create') await load();
      feedback(message);
    });
  }

  async function delegatedClick(event) {
    const button = event.target.closest('button[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'open-inbox') {
      await pollInbox();
      showInbox();
      return;
    }
    if (action === 'close-inbox') { $('live-dialog').close(); return; }
    if (action === 'read-inbox') {
      await task(button, async () => {
        await api(`/api/notifications/${validId(button.dataset.id)}/read`, 'POST', {});
        const target = button.dataset.target || 'overview';
        $('live-dialog').close();
        await pollInbox();
        showView(target, { historyMode: 'push', scroll: true, animate: true, focus: true });
      });
      return;
    }
    if (action === 'dismiss-payment-alert') {
      $('payment-alert-toast').hidden = true;
      return;
    }
    if (action === 'open-payment-alerts') {
      const toast = $('payment-alert-toast');
      if (toast) toast.hidden = true;
      showView('admin-claims', { historyMode: 'push', scroll: true, animate: true, focus: true });
      animateNavSelection('admin-finance');
      await task(button, async () => {
        feedback('กำลังโหลดรายการแจ้งชำระ…', false, true);
        await load();
        showView('admin-claims', { scroll: true, focus: true });
        feedback('');
      });
      return;
    }
    if (action === 'copy-payment-account') {
      await task(button, async () => {
        if (!state.paymentAccount?.account_number) throw new Error('ยังไม่มีเลขบัญชีรับโอน');
        await navigator.clipboard.writeText(state.paymentAccount.account_number.replace(/\D/g, ''));
        feedback('คัดลอกเลขบัญชีแล้ว');
      });
      return;
    }
    if (action === 'copy-payment-amount') {
      await task(button, async () => {
        const invoice = (state.data?.invoices || []).find((item) => item.id === state.paymentInvoiceId);
        if (!invoice) throw new Error('ไม่พบยอดที่ต้องชำระ');
        const satang = invoice.amount_satang - invoice.paid_satang;
        if (satang <= 0) throw new Error('ใบแจ้งยอดนี้ชำระครบแล้ว');
        await navigator.clipboard.writeText((satang / 100).toFixed(2));
        feedback('คัดลอกยอดที่ต้องชำระแล้ว');
      });
      return;
    }
    if (action === 'open-student-payment') {
      showStudentPayment(validId(button.dataset.id));
      showView('student-payments', { historyMode: 'push', scroll: true, animate: true, focus: true });
      return;
    }
    if (action === 'payment-back') {
      state.paymentInvoiceId = '';
      clearStudentSlip();
      showView('overview', { historyMode: 'push', scroll: true, animate: true, focus: true });
      return;
    }
    if (action === 'retry-sheet-sync') {
      await task(button, async () => {
        state.sheetSyncStatus = await api('/api/admin/sheet-sync/retry', 'POST', {});
        renderSheetSyncStatus();
        feedback(`ตรวจการซิงก์แล้ว · เหลือ ${Number(state.sheetSyncStatus.pending || 0)} รายการ`);
      });
      return;
    }
    if (action === 'preview-individual-billing') {
      await task(button, async () => { await loadIndividualBilling(true); });
      return;
    }
    if (action === 'issue-individual-billing') {
      await task(button, async () => {
        const preview = state.individualBilling;
        const period = $('individual-billing-period')?.value;
        const items = (preview?.items || []).filter((item) => !item.already_issued && Number(item.minutes) > 0);
        if (state.me.role !== 'admin' || !preview?.ready || period !== preview.period || !items.length) throw new Error('กรุณาตรวจยอดล่าสุดก่อนออกใบแจ้งยอด');
        const amount = items.reduce((sum, item) => sum + Number(item.amount_satang || 0), 0);
        if (!confirm(`ยืนยันออกใบแจ้งยอดค่าเรียนเดี่ยวเดือน ${monthlyPeriodLabel(period)} จำนวน ${items.length} รายการ รวม ${thaiMoney(amount)}?`)) return;
        const result = await api('/api/admin/individual-billing', 'POST', { period });
        state.individualBilling = null;
        await load();
        await loadIndividualBilling(true);
        feedback(`ออกใบแจ้งยอดรายเดือน ${Number(result.issued?.length || 0)} รายการแล้ว นักเรียนจะเห็นยอดในบัญชีของตน`);
      });
      return;
    }
    if (action === 'export-report') {
      await task(button, async () => { const count = await exportReport(); feedback(`ดาวน์โหลดข้อมูล ${count} รายการแล้ว`); });
      return;
    }
    if (action === 'jump') {
      const fromNav = Boolean(button.closest('#side-nav,#mobile-nav'));
      if (showView(button.dataset.target, { historyMode: 'push', scroll: true, animate: true, focus: !fromNav })) animateNavSelection(state.viewId);
      return;
    }
    if (action === 'dismiss-class-alert') {
      const region = $('student-live-cancellation');
      if (region) { region.hidden = true; region.replaceChildren(); }
      return;
    }
    if (action === 'select-time') {
      setTeacherTime(button.dataset.id);
      if (showView('teacher-time', { historyMode: 'push', scroll: true, animate: true, focus: true })) animateNavSelection('teacher-time');
      return;
    }
    if (action === 'staff-add') {
      await task(button, async () => {
        const answer = await fieldsDialog('เพิ่มครูผู้สอน', [
          { name: 'name', label: 'ชื่อครู', type: 'text', required: true, maxLength: 120 }
        ], 'เพิ่มครู', 'ระบบสร้างรหัสครูให้โดยอัตโนมัติ ครูยังเข้าใช้งานไม่ได้จนกว่าผู้ดูแลจะระบุอีเมลจริงและเปิดบัญชี');
        if (!answer) return;
        await api('/api/admin/staff', 'POST', { name: answer.name.trim() });
        await load(); feedback('เพิ่มครูในทะเบียนแล้ว');
      });
      return;
    }
    await task(button, async () => {
      const id = validId(button.dataset.id);
      let message = 'บันทึกข้อมูลแล้ว';
      switch (action) {
        case 'enrollment-cancel':
        case 'enrollment-restore':
          if (!confirm(action === 'enrollment-cancel' ? 'ยกเลิกการสมัครเรียนนี้หรือไม่? หากมีห้องเรียนในอนาคต ต้องนำออกจากห้องก่อน' : 'คืนสถานะการสมัครเรียนนี้หรือไม่?')) return;
          await api(`/api/admin/enrollments/${id}/${action === 'enrollment-cancel' ? 'cancel' : 'restore'}`, 'POST', {});
          message = action === 'enrollment-cancel' ? 'ยกเลิกการสมัครเรียนแล้ว' : 'คืนสถานะการสมัครเรียนแล้ว'; break;
        case 'class-detail':
          classDetailDialog(id); return;
        case 'class-edit':
          classEditDialog(id); return;
        case 'class-cancel':
          if (!confirm('ยกเลิกห้องเรียนนี้หรือไม่? ตารางเรียนของนักเรียนและครูจะเปลี่ยนตาม')) return;
          await api(`/api/admin/classes/${id}/cancel`, 'POST', {});
          message = 'ยกเลิกห้องเรียนแล้ว'; break;
        case 'availability-cancel':
          if (!confirm('นำช่วงเวลาว่างนี้ออกหรือไม่?')) return;
          await api(`/api/admin/teacher-availability/${id}/cancel`, 'POST', {});
          message = 'นำช่วงเวลาว่างออกแล้ว'; break;
        case 'task-edit': {
          const item = (state.tasks || []).find((entry) => entry.id === id && entry.status === 'open');
          if (!item) throw new Error('ไม่พบงานที่แก้ไขได้');
          const answer = await fieldsDialog(`แก้งาน · ${item.title}`, [
            { name: 'title', label: 'ชื่องาน', type: 'text', required: true, maxLength: 160, value: item.title },
            { name: 'description', label: 'รายละเอียด', type: 'textarea', maxLength: 2000, value: item.description },
            { name: 'due_date', label: 'กำหนดส่ง', type: 'date', value: item.due_date }
          ], 'บันทึกการแก้ไข');
          if (!answer) return;
          await api(`/api/admin/tasks/${id}`, 'PUT', { title: answer.title.trim(), description: answer.description.trim(), due_date: answer.due_date || null });
          message = 'แก้งานแล้ว ผู้รับงานจะได้รับแจ้งเตือน'; break;
        }
        case 'task-complete':
          if (!confirm('ยืนยันว่างานนี้เสร็จแล้วหรือไม่?')) return;
          await api(`/api/tasks/${id}/complete`, 'POST', {});
          message = 'บันทึกว่างานเสร็จแล้ว'; break;
        case 'task-cancel':
          if (!confirm('ยกเลิกงานที่มอบหมายนี้หรือไม่?')) return;
          await api(`/api/admin/tasks/${id}/cancel`, 'POST', {});
          message = 'ยกเลิกงานแล้ว ผู้รับงานจะได้รับแจ้งเตือน'; break;
        case 'teacher-cancel-class': {
          if (state.me.role !== 'teacher') throw new Error('เฉพาะครูผู้สอนที่ได้รับมอบหมายเท่านั้น');
          const session = (state.data.sessions || []).find((item) => item.id === id);
          if (!session || !session.can_cancel || Number(session.starts_at) <= Date.now() / 1000) throw new Error('คาบนี้ไม่สามารถยกคลาสได้แล้ว กรุณารีเฟรชหน้าหรือติดต่อผู้ดูแล');
          const answer = await fieldsDialog(`ยกคลาส · ${session.title}`, [
            { name: 'reason', label: 'เหตุผลที่แจ้งให้นักเรียนทราบ', type: 'textarea', required: true, minLength: 3, maxLength: 300 }
          ], 'ยืนยันยกคลาสและแจ้งนักเรียน', `${thaiDateTime(session.starts_at)} – ${thaiDateTime(session.ends_at)} · ระบบจะแสดงประกาศในหน้าของนักเรียนที่อยู่ในห้อง และส่งการแจ้งเตือนทางอีเมลตามการตั้งค่าของร้าน`);
          if (!answer) return;
          const reason = answer.reason.trim();
          if (reason.length < 3) throw new Error('กรุณาระบุเหตุผลอย่างน้อย 3 ตัวอักษร');
          await api(`/api/teacher/classes/${id}/cancel`, 'POST', { reason });
          message = 'ยกคลาสแล้ว นักเรียนในห้องจะเห็นเหตุผลในตารางเรียน'; break;
        }
        case 'student-enroll':
          {
            const course = state.courses.find((item) => item.id === id);
            if (!course) throw new Error('ไม่พบคอร์สที่เลือก');
            const profile = state.data.profile || {};
            const answer = await fieldsDialog(`ยืนยันสมัคร ${course.title}`, [
              { name: 'grade', label: 'ระดับชั้น', type: 'select', required: true, value: profile.grade, options: [['M1','ม.1'],['M2','ม.2'],['M3','ม.3'],['M4','ม.4'],['M5','ม.5'],['M6','ม.6']] },
              { name: 'school', label: 'โรงเรียน', type: 'text', required: true, maxLength: 150, value: profile.school },
              { name: 'guardian_name', label: 'ชื่อ–นามสกุลผู้ปกครอง', type: 'text', required: true, maxLength: 120, value: profile.guardian_name },
              { name: 'guardian_phone', label: 'เบอร์โทรศัพท์ผู้ปกครอง', type: 'tel', required: true, maxLength: 24, value: profile.guardian_phone },
              { name: 'guardian_relationship', label: 'ความสัมพันธ์กับนักเรียน', type: 'select', required: true, value: profile.guardian_relationship, options: [['father','บิดา'],['mother','มารดา'],['guardian','ผู้ปกครอง'],['other','อื่น ๆ']] }
            ], course.course_format === 'individual' ? 'ยืนยันสมัครเรียนเดี่ยว' : 'ยืนยันซื้อคอร์ส', course.course_format === 'individual'
              ? `อัตรา ${thaiMoney(INDIVIDUAL_RATE_SATANG_PER_HOUR)} ต่อชั่วโมงสอนจริง ผู้ดูแลจะจัดครูและตารางเรียน เมื่อสิ้นเดือนจะสรุปชั่วโมงที่สอนและออกใบแจ้งยอด ไม่ต้องชำระตอนสมัคร ข้อมูลผู้ปกครองใช้ติดต่อเรื่องการเรียนเท่านั้น`
              : `ราคาที่ผู้ดูแลกำหนด ${thaiMoney(course.price_satang)} · เมื่อยืนยันจะเปิดใบแจ้งยอดและพาไปหน้าโอนเงินทันที ข้อมูลผู้ปกครองใช้ติดต่อเรื่องการเรียนเท่านั้น`);
            if (!answer) return;
            const purchase = await api('/api/student/enrollments', 'POST', { course_id: id, ...answer, confirm_purchase: true });
            if (course.course_format === 'individual') {
              state.paymentInvoiceId = '';
              message = 'สมัครเรียนเดี่ยวแล้ว ผู้ดูแลจะจัดตาราง และสรุปยอดตามชั่วโมงสอนจริงเมื่อสิ้นเดือน';
            } else {
              state.paymentInvoiceId = validId(purchase.invoice_id);
              message = 'สมัครเรียนแล้ว ตรวจยอดและแนบสลิปได้ในหน้านี้';
            }
            break;
          }
        case 'admin-profile':
          await adminProfileDialog(id); return;
        case 'edit-user': {
          const person = (state.data.users || []).find((item) => item.id === id);
          if (!person || person.role === 'admin') throw new Error('ไม่พบบัญชีที่แก้ไขได้');
          const fields = [{ name: 'name', label: 'ชื่อ–นามสกุล', type: 'text', required: true, maxLength: 120, value: person.name }];
          if (!person.roster_pending) fields.push({ name: 'email', label: 'อีเมล', type: 'email', required: true, maxLength: 254, value: person.email });
          const answer = await fieldsDialog(`แก้ไขบัญชี ${personalCode(person)}`, fields,
            'บันทึกบัญชี', person.roster_pending ? 'เปิดบัญชีครูจากปุ่มในทะเบียนเมื่อมีอีเมลจริง' : 'รหัสประจำตัวจะคงเดิม การเปลี่ยนอีเมลมีผลต่อการแจ้งเตือนในครั้งถัดไป');
          if (!answer) return;
          await api(`/api/admin/users/${id}`, 'PUT', person.roster_pending ? { name: answer.name.trim() } : { name: answer.name.trim(), email: answer.email.trim() });
          message = 'แก้ไขข้อมูลครูหรือนักเรียนแล้ว'; break;
        }
        case 'staff-activate': {
          const person = (state.data.users || []).find((item) => item.id === id && item.role === 'teacher' && item.roster_pending);
          if (!person) throw new Error('ไม่พบครูที่รอเปิดบัญชี');
          const answer = await fieldsDialog(`เปิดบัญชี ${person.name}`, [
            { name: 'email', label: 'อีเมลจริงของครู', type: 'email', required: true, maxLength: 254 }
          ], 'เปิดบัญชีครู', 'ระบบจะสร้างรหัสผ่านชั่วคราวให้แสดงครั้งเดียวและบังคับครูเปลี่ยนรหัสเมื่อเข้าสู่ระบบ');
          if (!answer) return;
          const created = await api(`/api/admin/staff/${id}/activate`, 'POST', { email: answer.email.trim() });
          await load(); showCredential(created); feedback('เปิดบัญชีครูแล้ว'); return;
        }
        case 'course-edit': {
          const course = (state.academic?.courses || []).find((item) => item.id === id);
          if (!course) throw new Error('ไม่พบคอร์ส');
          const teacherOptions = (state.data.users || [])
            .filter((person) => person.role === 'teacher' && (person.status === 'active' || person.roster_pending || person.id === course.teacher_id))
            .map((person) => [person.id, `${person.teacher_code || '—'} · ${person.name}${person.login_enabled ? '' : ' · ยังไม่เปิดบัญชี'}`]);
          const answer = await fieldsDialog(`แก้ไขคอร์ส ${course.course_code}`, [
            ...(course.course_format === 'individual' ? [] : [{ name: 'price', label: 'ราคาคอร์ส (บาท)', type: 'text', required: true, maxLength: 12, value: (Number(course.price_satang) / 100).toFixed(2) }]),
            { name: 'teacher_id', label: 'ครูประจำคอร์ส', type: 'select', value: course.teacher_id, options: [['', 'ยังไม่กำหนด — จัดครูภายหลัง'], ...teacherOptions] },
            { name: 'capacity', label: 'จำนวนนักเรียนสูงสุด', type: 'number', required: true, value: course.capacity },
            { name: 'target_sessions', label: 'จำนวนคาบเป้าหมาย', type: 'number', required: true, value: course.target_sessions || 12 },
            { name: 'active', label: 'สถานะเปิดรับ', type: 'select', required: true, value: course.active ? 'true' : 'false', options: [['true', 'เปิดรับสมัคร'], ['false', 'ปิดรับสมัคร']] }
          ], 'บันทึกคอร์ส', course.course_format === 'individual'
            ? 'เรียนเดี่ยวใช้อัตรา 300 บาทต่อชั่วโมงจริงคงที่ ผู้ดูแลจัดครูและตารางได้จากหน้านี้'
            : 'รายวิชา ระดับ และรหัสคอร์สคงเดิม ราคาใหม่จะแสดงในหน้าสมัครเรียน แต่ใบแจ้งยอดที่ออกแล้วไม่เปลี่ยนตาม');
          if (!answer) return;
          await api(`/api/admin/courses/${id}`, 'PUT', {
            price_satang: course.course_format === 'individual' ? INDIVIDUAL_RATE_SATANG_PER_HOUR : toSatang(answer.price), teacher_id: answer.teacher_id ? validId(answer.teacher_id) : null, capacity: Number(answer.capacity), target_sessions: Number(answer.target_sessions), active: answer.active === 'true'
          });
          message = 'แก้ไขคอร์สแล้ว'; break;
        }
        case 'reset-user': {
          if (!confirm('รีเซ็ตรหัสผ่านและออกจากระบบทุกอุปกรณ์ของบัญชีนี้หรือไม่?')) return;
          const person = (state.data.users || []).find((item) => item.id === id);
          const result = await api(`/api/admin/users/${id}/reset-password`, 'POST', {});
          await load(); showCredential({ user: person, temporary_password: result.temporary_password });
          feedback('สร้างรหัสชั่วคราวแล้ว'); return;
        }
        case 'disable-user':
        case 'enable-user':
          if (!confirm(action === 'disable-user' ? 'ปิดบัญชีและออกจากระบบทุกอุปกรณ์หรือไม่?' : 'เปิดให้บัญชีนี้ใช้งานอีกครั้งหรือไม่?')) return;
          await api(`/api/admin/users/${id}/${action === 'disable-user' ? 'disable' : 'enable'}`, 'POST', {});
          message = action === 'disable-user' ? 'ปิดบัญชีแล้ว' : 'เปิดบัญชีแล้ว'; break;
        case 'roster':
          await rosterDialog(id); return;
        case 'payroll-detail':
          await payrollDetailDialog(id); return;
        case 'edit-invoice-amount': {
          const invoice = (state.data.invoices || []).find((item) => item.id === id);
          if (!invoice) throw new Error('ไม่พบใบแจ้งยอด');
          const answer = await fieldsDialog(`แก้ยอดใบแจ้งยอด ${invoice.invoice_number}`, [
            { name: 'amount', label: 'ค่าเรียนใหม่ (บาท)', type: 'text', required: true, value: (invoice.amount_satang / 100).toFixed(2) }
          ], 'บันทึกยอดใหม่', 'แก้ได้ก่อนมีการรับเงินหรือรายการแจ้งชำระที่รอตรวจ');
          if (!answer) return;
          await api(`/api/admin/invoices/${id}/amount`, 'POST', { amount_satang: toSatang(answer.amount) });
          message = 'แก้ยอดค่าเรียนแล้ว'; break;
        }
        case 'edit-invoice-due-date': {
          const invoice = (state.data.invoices || []).find((item) => item.id === id);
          if (!invoice) throw new Error('ไม่พบใบแจ้งยอด');
          const answer = await fieldsDialog(`เปลี่ยนวันครบกำหนด ${invoice.invoice_number}`, [
            { name: 'due_date', label: 'วันครบกำหนดใหม่', type: 'date', required: true, value: invoice.due_date }
          ], 'บันทึกวันครบกำหนด');
          if (!answer) return;
          await api(`/api/admin/invoices/${id}/due-date`, 'POST', { due_date: answer.due_date });
          message = 'เปลี่ยนวันครบกำหนดแล้ว'; break;
        }
        case 'void-invoice':
          if (!confirm('ยกเลิกใบแจ้งยอดนี้หรือไม่? นักเรียนจะไม่ต้องชำระใบนี้แล้ว')) return;
          await api(`/api/admin/invoices/${id}/void`, 'POST', {});
          message = 'ยกเลิกใบแจ้งยอดแล้ว'; break;
        case 'restore-invoice':
          if (!confirm('คืนสถานะใบแจ้งยอดนี้และแจ้งนักเรียนอีกครั้งหรือไม่?')) return;
          await api(`/api/admin/invoices/${id}/restore`, 'POST', {});
          message = 'คืนสถานะใบแจ้งยอดแล้ว'; break;
        case 'verify-claim': {
          const review = await claimVerificationDialog(id);
          if (!review) return;
          const claim = (state.data?.pending_payment_claims || []).find((item) => item.id === id);
          if (claim) {
            claim.verification_status = review.verification?.status || 'needs_review';
            claim.precheck_status = review.precheck?.status || 'failed';
            renderWorkspace();
          }
          message = review.verification?.status === 'correct'
            ? 'บันทึกว่าตรวจสลิปและยอดในบัญชีแล้ว กรุณากดยืนยันรับเงินเพื่อออกใบสำคัญรับเงิน'
            : 'บันทึกว่าสลิปไม่ถูกต้องแล้ว กรุณากดปฏิเสธเพื่อแจ้งนักเรียน';
          feedback(message); return;
        }
        case 'approve-claim': {
          const current = await api(`/api/admin/payment-claims/${id}/verification`);
          if (!current.can_approve) throw new Error('ต้องตรวจสลิปเบื้องต้นและยืนยันยอดเงินเข้าบัญชีจริงก่อน');
          if (!confirm('ตรวจสลิปและยอดเงินที่เข้าบัญชีจริงครบแล้วใช่หรือไม่?')) return;
          await api(`/api/admin/payment-claims/${id}/approve`, 'POST', {}); message = 'ยืนยันรับเงินแล้ว'; break;
        }
        case 'reject-claim': {
          const current = await api(`/api/admin/payment-claims/${id}/verification`);
          if (current.verification?.status !== 'incorrect') throw new Error('ต้องบันทึกผลตรวจว่าไม่ถูกต้องพร้อมเหตุผลก่อน');
          if (!confirm('ปฏิเสธรายการแจ้งชำระนี้หรือไม่?')) return;
          await api(`/api/admin/payment-claims/${id}/reject`, 'POST', {}); message = 'ปฏิเสธรายการแจ้งชำระแล้ว'; break;
        }
        case 'approve-time':
          await api(`/api/admin/time-entries/${id}/approve`, 'POST', {}); message = 'อนุมัติเวลาสอนและคำนวณค่าจ้างแล้ว'; break;
        case 'override-time': {
          const answer = await fieldsDialog('อนุมัติยอดพิเศษรายคาบ', [
            { name: 'amount', label: 'ยอดค่าจ้างคาบนี้ (บาท)', type: 'text', required: true },
            { name: 'reason', label: 'เหตุผลที่ไม่ใช้อัตราปกติ', type: 'text', required: true, maxLength: 250 },
            { name: 'source_reference', label: 'เอกสารหรือแถวในชีตอ้างอิง', type: 'text', required: true, maxLength: 120 }
          ], 'อนุมัติยอดพิเศษ');
          if (!answer) return;
          await api(`/api/admin/time-entries/${id}/approve-override`, 'POST', {
            amount_satang: toSatang(answer.amount), reason: answer.reason.trim(), source_reference: answer.source_reference.trim()
          });
          message = 'อนุมัติยอดพิเศษพร้อมบันทึกเหตุผลแล้ว'; break;
        }
        case 'reject-time': {
          const answer = await fieldsDialog('ปฏิเสธเวลาสอน', [{ name: 'reason', label: 'เหตุผล', type: 'text', required: true, maxLength: 250 }], 'ยืนยันการปฏิเสธ');
          if (!answer) return;
          await api(`/api/admin/time-entries/${id}/reject`, 'POST', { reason: answer.reason.trim() }); message = 'ปฏิเสธเวลาสอนแล้ว'; break;
        }
        case 'confirm-rate': {
          const rule = (state.data.rates || []).find((item) => item.id === id);
          const teachers = (state.data.staff || []).filter((person) => person.role === 'teacher' && (person.status === 'active' || person.roster_pending));
          const answer = await fieldsDialog(`ยืนยันอัตรา ${rule?.teacher_label || ''}`, [
            { name: 'teacher_id', label: 'จับคู่กับครู', type: 'select', required: true, options: teachers.map((person) => [person.id, `${person.name} · ${person.email || 'รอเปิดบัญชี'}`]) },
            { name: 'effective_from', label: 'วันที่เริ่มใช้', type: 'date', required: true }
          ], 'ยืนยันอัตรานี้');
          if (!answer) return;
          const teacher = teachers.find((person) => person.id === answer.teacher_id);
          const payload = { teacher_id: validId(answer.teacher_id), effective_from: answer.effective_from };
          if (teacher && teacher.name.trim().toLocaleLowerCase() !== String(rule?.teacher_label || '').trim().toLocaleLowerCase()) {
            const mismatch = await fieldsDialog('ชื่อครูในชีตไม่ตรงกับบัญชี', [
              { name: 'reason', label: 'เหตุผล/หลักฐานที่ยืนยันว่าเป็นคนเดียวกัน', type: 'text', required: true, maxLength: 250 }
            ], 'ยืนยันการจับคู่');
            if (!mismatch) return;
            payload.confirm_name_mismatch = true;
            payload.reason = mismatch.reason.trim();
          }
          await api(`/api/admin/rates/${id}/confirm`, 'POST', payload);
          message = 'ยืนยันอัตราค่าจ้างแล้ว'; break;
        }
        case 'end-rate': {
          const answer = await fieldsDialog('สิ้นสุดอัตราค่าจ้าง', [{ name: 'effective_to', label: 'วันที่สิ้นสุดการใช้', type: 'date', required: true }], 'บันทึกวันสิ้นสุด');
          if (!answer) return;
          await api(`/api/admin/rates/${id}/end`, 'POST', { effective_to: answer.effective_to });
          message = 'บันทึกวันสิ้นสุดอัตราแล้ว'; break;
        }
        case 'adjust-payroll': {
          const answer = await fieldsDialog('ปรับยอดงวดค่าจ้าง', [
            { name: 'amount', label: 'จำนวนเงินเพิ่ม/ลด (บาท; ใส่ - หากลด)', type: 'text', required: true },
            { name: 'reason', label: 'เหตุผล', type: 'text', required: true, maxLength: 250 }
          ], 'บันทึกการปรับยอด');
          if (!answer) return;
          await api(`/api/admin/payroll-runs/${id}/adjustments`, 'POST', { amount_satang: toSatang(answer.amount, true), reason: answer.reason.trim() });
          message = 'ปรับยอดงวดแล้ว'; break;
        }
        case 'finalize-payroll':
          if (!confirm('ตรวจยอดและต้องการปิดงวดนี้หรือไม่? หลังปิดงวดจะแก้ไม่ได้')) return;
          await api(`/api/admin/payroll-runs/${id}/finalize`, 'POST', {}); message = 'ปิดงวดค่าจ้างแล้ว'; break;
        case 'cancel-payroll':
          if (!confirm('ยกเลิกงวดร่างนี้หรือไม่? รายการเวลาสอนที่อนุมัติจะกลับไปสร้างงวดใหม่ได้')) return;
          await api(`/api/admin/payroll-runs/${id}/cancel`, 'POST', {}); message = 'ยกเลิกงวดร่างแล้ว'; break;
        case 'pay-payroll': {
          const answer = await fieldsDialog('บันทึกการจ่ายค่าจ้าง', [{ name: 'reference', label: 'เลขอ้างอิงการโอน', type: 'text', required: true, maxLength: 120 }], 'ยืนยันว่าจ่ายแล้ว');
          if (!answer) return;
          await api(`/api/admin/payroll-runs/${id}/mark-paid`, 'POST', { reference: answer.reference.trim() });
          message = 'บันทึกว่าจ่ายค่าจ้างแล้ว'; break;
        }
        default: throw new Error('ยังไม่รองรับการดำเนินการนี้');
      }
      await load();
      if (action === 'student-enroll' && state.paymentInvoiceId) {
        showView('student-payments', { historyMode: 'push', scroll: true, animate: true, focus: true });
      }
      feedback(message);
    });
  }

  function fieldsDialog(title, fields, submitText, note = '') {
    const dialog = $('live-dialog');
    dialog.replaceChildren();
    const form = element('form', 'live-form');
    const heading = element('h2', '', title);
    heading.id = 'live-dialog-title';
    dialog.setAttribute('aria-labelledby', heading.id);
    form.append(heading);
    if (note) form.append(element('p', 'muted', note));
    for (const field of fields) {
      const label = element('label', '', field.label);
      let input;
      if (field.type === 'select') {
        input = element('select');
        if (!(field.options || []).some(([value]) => value === '')) option(input, '', 'เลือกข้อมูล');
        for (const [value, name] of field.options || []) option(input, value, name);
      } else if (field.type === 'textarea') {
        input = element('textarea'); input.rows = 4;
      } else {
        input = element('input'); input.type = field.type || 'text';
      }
      if (field.minLength) input.minLength = field.minLength;
      if (field.maxLength) input.maxLength = field.maxLength;
      input.name = field.name; input.required = Boolean(field.required); label.append(input); form.append(label);
      if (field.value) input.value = field.value;
    }
    const actions = element('div', 'actions');
    const cancel = element('button', '', 'ยกเลิก'); cancel.type = 'button';
    const save = element('button', 'primary', submitText); save.type = 'submit';
    actions.append(cancel, save); form.append(actions); dialog.append(form);
    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => { if (!settled) { settled = true; resolve(value); } };
      cancel.onclick = () => dialog.close();
      form.onsubmit = (event) => { event.preventDefault(); const values = Object.fromEntries(new FormData(form)); dialog.close(); finish(values); };
      dialog.onclose = () => { dialog.removeAttribute('aria-labelledby'); dialog.replaceChildren(); finish(null); };
      dialog.showModal();
    });
  }

  function classDetailDialog(classId) {
    const classroom = state.classes.find((item) => item.id === classId);
    if (!classroom) throw new Error('ไม่พบห้องเรียน');
    const dialog = $('live-dialog'); dialog.replaceChildren(); dialog.classList.add('class-dialog');
    dialog.append(element('h2', '', `${classroom.room_name} · ${classroom.course_title}`));
    dialog.append(element('p', 'muted', `${classroom.course_code || '—'} · ${classSummary(classroom)}`));
    dialog.append(element('p', '', `ผู้สอน ${classroom.teacher_code || '—'} · ${classroom.teacher_name || '—'} · ${status(classroom.status)}`));
    if (classroom.status === 'cancelled' && cancellationReason(classroom)) dialog.append(element('p', 'class-cancel-reason', `เหตุผลที่ยกคลาส: ${cancellationReason(classroom)}`));
    dialog.append(element('h3', '', `นักเรียนในห้อง (${classroom.roster_count ?? classroom.students?.length ?? 0} คน)`));
    const wrap = element('div', 'tablewrap'); const table = element('table');
    const head = element('thead'); const headerRow = element('tr');
    for (const label of ['รหัสนักเรียน', 'ชื่อ', 'การเข้าเรียน']) headerRow.append(element('th', '', label));
    head.append(headerRow); table.append(head);
    const body = element('tbody');
    if (!(classroom.students || []).length) emptyRow(body, 3, 'ยังไม่ได้จัดนักเรียนเข้าห้อง');
    for (const person of classroom.students || []) {
      const row = element('tr');
      cell(row, person.student_code || '—', 'record-code');
      cell(row, person.name);
      cell(row, person.attendance_status === 'present' ? 'มาเรียน' : person.attendance_status === 'absent' ? 'ขาดเรียน' : 'ยังไม่เช็กชื่อ');
      body.append(row);
    }
    table.append(body); wrap.append(table); dialog.append(wrap);
    const close = element('button', 'primary class-dialog-close', 'ปิด'); close.type = 'button';
    close.onclick = () => dialog.close(); dialog.append(close);
    dialog.onclose = () => { dialog.classList.remove('class-dialog'); dialog.replaceChildren(); };
    dialog.showModal();
  }

  function classEditDialog(classId) {
    const classroom = state.classes.find((item) => item.id === classId);
    if (!classroom) throw new Error('ไม่พบห้องเรียน');
    const dialog = $('live-dialog'); dialog.replaceChildren(); dialog.classList.add('class-dialog');
    const form = element('form', 'live-form');
    form.append(element('h2', '', 'แก้ไขห้องเรียน'));
    form.append(element('p', 'muted', 'ผู้ดูแลเป็นผู้เลือกคอร์ส ครู ห้อง เวลา และนักเรียนทุกคนด้วยตนเอง'));
    const addField = (labelText, name, type, value = '') => {
      const label = element('label', '', labelText);
      const input = element(type === 'select' ? 'select' : 'input');
      if (type !== 'select') input.type = type;
      input.name = name; input.required = true; input.value = value;
      label.append(input); form.append(label); return input;
    };
    const courseInput = addField('คอร์ส / รหัสวิชา', 'course_id', 'select');
    for (const course of state.academic?.courses || []) {
      if (course.active || course.id === classroom.course_id) option(courseInput, course.id, `${course.course_code || '—'} · ${course.title}`);
    }
    courseInput.value = classroom.course_id;
    const teacherInput = addField('ครูผู้สอน', 'teacher_id', 'select');
    for (const teacher of state.data.users || []) {
      if (teacher.role === 'teacher' && (teacher.status === 'active' || teacher.roster_pending || teacher.id === classroom.teacher_id)) option(teacherInput, teacher.id, `${teacher.teacher_code || '—'} · ${teacher.name}`);
    }
    teacherInput.value = classroom.teacher_id;
    const roomInput = addField('ชื่อห้อง', 'room_name', 'select');
    for (const room of state.academic?.rooms || []) option(roomInput, room, room);
    if (classroom.room_name && ![...(state.academic?.rooms || [])].includes(classroom.room_name)) {
      option(roomInput, classroom.room_name, `${classroom.room_name} (ห้องเดิม)`);
    }
    roomInput.value = classroom.room_name;
    const startInput = addField('เริ่มเรียน (เวลาไทย)', 'starts_at', 'datetime-local', bangkokInput(epochValue(classroom.starts_at)));
    const endInput = addField('เลิกเรียน (เวลาไทย)', 'ends_at', 'datetime-local', bangkokInput(epochValue(classroom.ends_at)));
    const fieldset = element('fieldset', 'class-roster-field');
    fieldset.append(element('legend', '', 'นักเรียนในห้อง'));
    const picker = element('div', 'class-student-picker');
    fieldset.append(picker); form.append(fieldset);
    const currentIds = (classroom.students || []).map((person) => person.id);
    fillClassStudentPicker(classroom.course_id, currentIds, picker);
    courseInput.addEventListener('change', () => {
      const course = (state.academic?.courses || []).find((item) => item.id === courseInput.value);
      teacherInput.value = course?.teacher_id || '';
      fillClassStudentPicker(courseInput.value, courseInput.value === classroom.course_id ? currentIds : [], picker);
    });
    const actions = element('div', 'actions');
    const cancel = element('button', '', 'ยกเลิก'); cancel.type = 'button'; cancel.onclick = () => dialog.close();
    const save = element('button', 'primary', 'บันทึกห้องเรียน'); save.type = 'submit';
    actions.append(cancel, save); form.append(actions);
    const notice = element('p', 'live-modal-feedback'); notice.setAttribute('role', 'alert'); form.append(notice);
    dialog.append(form);
    form.onsubmit = async (event) => {
      event.preventDefault(); save.disabled = true; notice.textContent = '';
      try {
        await api(`/api/admin/classes/${classId}`, 'PUT', {
          course_id: validId(courseInput.value), teacher_id: validId(teacherInput.value), room_name: roomInput.value.trim(),
          starts_at: bangkokISO(startInput.value), ends_at: bangkokISO(endInput.value), student_ids: chosenStudentIds(form)
        });
        dialog.close(); await load(); feedback('บันทึกห้องเรียนและรายชื่อแล้ว');
      } catch (error) { notice.textContent = error.message || 'บันทึกไม่สำเร็จ'; save.disabled = false; }
    };
    dialog.onclose = () => { dialog.classList.remove('class-dialog'); dialog.replaceChildren(); };
    dialog.showModal();
  }

  async function adminProfileDialog(studentId) {
    const details = await api(`/api/admin/students/${studentId}/profile`);
    const profile = details.profile || {};
    const answer = await fieldsDialog(`ข้อมูลนักเรียน · ${details.student.name}`, [
      { name: 'grade', label: 'ระดับชั้น', type: 'select', required: true, value: profile.grade, options: [['M1','ม.1'],['M2','ม.2'],['M3','ม.3'],['M4','ม.4'],['M5','ม.5'],['M6','ม.6']] },
      { name: 'school', label: 'โรงเรียน', type: 'text', required: true, maxLength: 150, value: profile.school },
      { name: 'guardian_name', label: 'ชื่อ–นามสกุลผู้ปกครอง', type: 'text', required: true, maxLength: 120, value: profile.guardian_name },
      { name: 'guardian_phone', label: 'เบอร์โทรศัพท์ผู้ปกครอง', type: 'tel', required: true, maxLength: 24, value: profile.guardian_phone },
      { name: 'guardian_relationship', label: 'ความสัมพันธ์กับนักเรียน', type: 'select', required: true, value: profile.guardian_relationship, options: [['father','บิดา'],['mother','มารดา'],['guardian','ผู้ปกครอง'],['other','อื่น ๆ']] }
    ], 'บันทึกข้อมูล', 'ข้อมูลติดต่อผู้ปกครองเป็นข้อมูลส่วนตัว กรุณาเปิดดูเฉพาะเมื่อจำเป็น');
    if (!answer) return;
    await api(`/api/admin/students/${studentId}/profile`, 'PUT', answer);
    feedback('บันทึกข้อมูลนักเรียนแล้ว');
  }

  function showCredential(created) {
    const dialog = $('live-dialog');
    dialog.replaceChildren();
    dialog.append(element('h2', '', 'สร้างรหัสผ่านชั่วคราวแล้ว'));
    dialog.append(element('p', 'muted', `${personalCode(created.user)} · ${created.user.name} · ${created.user.email}`));
    dialog.append(element('p', '', 'รหัสผ่านชั่วคราว (แสดงครั้งเดียว):'));
    const secret = element('input', 'temporary-secret'); secret.value = created.temporary_password; secret.readOnly = true; secret.setAttribute('aria-label', 'รหัสผ่านชั่วคราว');
    dialog.append(secret);
    const copy = element('button', '', 'คัดลอกรหัสผ่าน'); copy.type = 'button';
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(secret.value); copy.textContent = 'คัดลอกแล้ว'; }
      catch { secret.focus(); secret.select(); copy.textContent = 'เลือกรหัสแล้ว กดคัดลอกด้วยตนเอง'; }
    };
    dialog.append(copy);
    dialog.append(element('p', 'foot', 'สำคัญ: รหัสนี้แสดงครั้งเดียว ส่งให้เจ้าของบัญชีด้วยช่องทางที่ปลอดภัย และผู้ใช้ต้องเปลี่ยนเมื่อเข้าสู่ระบบครั้งแรก'));
    const close = element('button', 'primary', 'ปิด'); close.type = 'button'; close.onclick = () => dialog.close(); dialog.append(close);
    dialog.onclose = () => { secret.value = ''; dialog.replaceChildren(); };
    dialog.showModal();
  }

  async function rosterDialog(sessionId) {
    let roster;
    try {
      roster = await api(`/api/${state.me.role}/sessions/${sessionId}/roster`);
    } catch (error) {
      if (error.status === 404) throw new Error('ระบบเช็กชื่อยังไม่เปิดในรุ่นนี้ กรุณาให้ผู้ดูแลเพิ่มบริการรายชื่อนักเรียนก่อนอนุมัติเวลา');
      throw error;
    }
    const students = roster.students || roster.roster || [];
    const dialog = $('live-dialog'); dialog.replaceChildren();
    const form = element('form', 'live-form'); form.append(element('h2', '', 'เช็กชื่อนักเรียน'));
    if (!students.length) form.append(element('p', 'muted', 'ยังไม่มีนักเรียนในคอร์สนี้'));
    const list = element('div', 'roster-list');
    for (const student of students) {
      const studentId = student.student_id || student.id;
      if (!/^[0-9a-f]{32}$/.test(String(studentId))) continue;
      const label = element('label', 'roster-item', `${student.student_code ? `${student.student_code} · ` : ''}${student.name || student.student_name || student.email || studentId}`);
      const select = element('select'); select.dataset.studentId = studentId; select.required = true;
      option(select, '', 'เลือกสถานะ'); option(select, 'present', 'เข้าเรียน'); option(select, 'absent', 'ขาดเรียน');
      const current = student.attendance_status || student.attendance || student.status;
      select.value = current === 'absent' || current === 'present' ? current : '';
      label.append(select); list.append(label);
    }
    form.append(list);
    const actions = element('div', 'actions'); const cancel = element('button', '', 'ยกเลิก'); cancel.type = 'button';
    const save = element('button', 'primary', 'บันทึกเช็กชื่อ'); save.type = 'submit'; save.disabled = !list.children.length;
    actions.append(cancel, save); form.append(actions); dialog.append(form);
    cancel.onclick = () => dialog.close();
    form.onsubmit = async (event) => {
      event.preventDefault(); save.disabled = true;
      try {
        const records = [...list.querySelectorAll('select')].map((select) => ({ student_id: select.dataset.studentId, status: select.value }));
        await api(`/api/${state.me.role}/sessions/${sessionId}/attendance`, 'PUT', { records });
        dialog.close(); await load(); feedback('บันทึกเช็กชื่อแล้ว');
      } catch (error) { feedback(error.message || 'บันทึกเช็กชื่อไม่สำเร็จ', true); save.disabled = false; }
    };
    dialog.onclose = () => dialog.replaceChildren();
    dialog.showModal();
  }

  async function payrollDetailDialog(runId) {
    const role = state.me.role;
    const detail = await api(`/api/${role}/payroll-runs/${runId}`);
    const run = detail.run;
    const dialog = $('live-dialog'); dialog.replaceChildren();
    dialog.append(element('h2', '', `งวด ${run.period} · ${run.teacher_name}`));
    dialog.append(element('p', 'muted', `สถานะ ${status(run.status)} · ยอดรวม ${thaiMoney(run.total_satang)}`));
    const wrap = element('div', 'tablewrap'); const table = element('table');
    const head = element('thead'); const headRow = element('tr');
    for (const label of ['วันที่ / คอร์ส', 'เวลาสอน', 'อัตรา/ที่มา', 'ยอด']) headRow.append(element('th', '', label));
    head.append(headRow); table.append(head);
    const body = element('tbody');
    for (const line of detail.lines || []) {
      const row = element('tr'); const course = cell(row, line.course_title); course.append(element('div', 'muted', thaiDateTime(line.starts_at)));
      cell(row, hours(line.minutes));
      const rule = line.pricing_type === 'manual_amount' ? 'ยอดพิเศษ' : `${thaiMoney(line.rate_satang_per_hour)} / ชม.`;
      const source = cell(row, rule);
      if (line.override_reference) source.append(element('div', 'muted', `อ้างอิง: ${line.override_reference}`));
      if (line.override_reason) source.append(element('div', 'muted', line.override_reason));
      cell(row, thaiMoney(line.amount_satang)); body.append(row);
    }
    if (!(detail.lines || []).length) emptyRow(body, 4, 'ไม่มีรายการในงวดนี้');
    table.append(body); wrap.append(table); dialog.append(wrap);
    if ((detail.adjustments || []).length) {
      dialog.append(element('h3', '', 'รายการปรับยอด'));
      for (const adjustment of detail.adjustments) dialog.append(element('p', 'foot', `${thaiMoney(adjustment.amount_satang)} · ${adjustment.reason}`));
    }
    if (run.payment_reference) dialog.append(element('p', 'foot', `อ้างอิงการจ่าย: ${run.payment_reference}`));
    const close = element('button', 'primary', 'ปิด'); close.type = 'button'; close.onclick = () => dialog.close(); dialog.append(close);
    dialog.onclose = () => dialog.replaceChildren();
    dialog.showModal();
  }

  function passwordDialog(forced) {
    const dialog = $('live-dialog'); dialog.replaceChildren();
    const form = element('form', 'live-form');
    form.append(element('h2', '', forced ? 'เปลี่ยนรหัสผ่านชั่วคราว' : 'เปลี่ยนรหัสผ่าน'));
    form.append(element('p', 'muted', forced ? 'ต้องตั้งรหัสผ่านใหม่ก่อนใช้งานบัญชีนี้' : 'หลังเปลี่ยนรหัส ระบบจะให้เข้าสู่ระบบอีกครั้ง'));
    for (const [name, label] of [['current_password', 'รหัสผ่านปัจจุบัน'], ['new_password', 'รหัสผ่านใหม่ (อย่างน้อย 12 ตัวอักษร)']]) {
      const wrapper = element('div', 'password-row');
      const inputLabel = element('label', '', label);
      const field = element('div', 'password-entry');
      const input = element('input');
      input.id = `password-${name}`;
      inputLabel.htmlFor = input.id;
      input.type = 'password'; input.name = name; input.required = true;
      input.minLength = name === 'new_password' ? 12 : 1;
      input.autocomplete = name === 'new_password' ? 'new-password' : 'current-password';
      const toggle = element('button', 'password-reveal');
      toggle.type = 'button';
      toggle.setAttribute('aria-controls', input.id);
      toggle.setAttribute('aria-label', `แสดง${label}`);
      toggle.setAttribute('aria-pressed', 'false');
      toggle.append(eyeIcon());
      toggle.onclick = () => {
        const shown = input.type === 'password';
        input.type = shown ? 'text' : 'password';
        toggle.setAttribute('aria-label', `${shown ? 'ซ่อน' : 'แสดง'}${label}`);
        toggle.setAttribute('aria-pressed', String(shown));
      };
      field.append(input, toggle); wrapper.append(inputLabel, field); form.append(wrapper);
    }
    const actions = element('div', 'actions');
    if (!forced) { const cancel = element('button', '', 'ยกเลิก'); cancel.type = 'button'; cancel.onclick = () => dialog.close(); actions.append(cancel); }
    const save = element('button', 'primary', 'บันทึกรหัสผ่านใหม่'); save.type = 'submit'; actions.append(save); form.append(actions);
    const notice = element('p', 'live-modal-feedback'); notice.setAttribute('role', 'alert'); form.append(notice); dialog.append(form);
    form.onsubmit = async (event) => {
      event.preventDefault(); save.disabled = true; notice.textContent = '';
      try {
        await api('/api/auth/change-password', 'POST', Object.fromEntries(new FormData(form)));
        location.replace('index.html');
      } catch (error) { notice.textContent = error.message || 'เปลี่ยนรหัสผ่านไม่สำเร็จ'; save.disabled = false; }
    };
    if (forced) dialog.addEventListener('cancel', (event) => event.preventDefault(), { once: true });
    dialog.onclose = () => { if (forced) passwordDialog(true); else dialog.replaceChildren(); };
    dialog.showModal();
  }

  async function start() {
    document.body.classList.add('live-ready', 'loading-screen');
    const bootstrap = document.getElementById('pp-auth-bootstrap');
    let me = null;
    if (bootstrap) {
      try { me = JSON.parse(bootstrap.textContent); }
      catch { /* Fall back to the authenticated API when markup is stale. */ }
    }
    document.body.innerHTML = '<main class="live-loading"><img src="physics-plus-logo-yellow-black.jpg" alt="Physics Plus"><p>กำลังตรวจสอบบัญชี…</p></main>';
    try {
      if (!me) me = await api('/api/me');
      if (!me.authenticated || !me.user) { location.replace('index.html'); return; }
      if (!roleNames[me.user.role]) throw new Error('บทบาทบัญชีไม่ถูกต้อง');
      state.me = me.user; state.csrf = me.csrf_token;
      shell(me.user);
      loadDisplayFontsAfterPaint();
      window.setTimeout(() => document.body.classList.add('dashboard-mounted'), 250);
      if (me.user.must_change_password) {
        $('live-content').append(element('section', 'panel', 'กรุณาเปลี่ยนรหัสผ่านชั่วคราวก่อนใช้งาน'));
        passwordDialog(true); return;
      }
      const loadingPanel = element('section', 'panel workspace-loading-panel');
      loadingPanel.setAttribute('aria-busy', 'true');
      loadingPanel.setAttribute('role', 'status');
      loadingPanel.append(element('strong', '', 'กำลังโหลดข้อมูลล่าสุด…'));
      loadingPanel.append(element('p', 'muted', 'กำลังเตรียมคอร์ส ตารางเรียน และข้อมูลบัญชีของคุณ'));
      const loadingRows = element('div', 'workspace-loading-rows');
      for (let index = 0; index < 3; index += 1) loadingRows.append(element('span', 'workspace-loading-row'));
      loadingPanel.append(loadingRows);
      $('live-content').append(loadingPanel);
      await load({ initial: true, overviewCounts: me.overview_counts || null });
      if (state.me.role === 'student') startStudentClassPolling();
      // Revision tracking can begin after the initial data is ready; it is
      // informational and should never compete with the first screen.
      void api('/api/sync/revision').then((result) => {
        if (state.revision === null) state.revision = result.revision ?? null;
      }).catch(() => {});
      void pollInbox();
    } catch (error) {
      document.body.innerHTML = '';
      const main = element('main', 'live-loading');
      main.append(element('h1', '', 'เปิดระบบไม่ได้'), element('p', '', error.message || 'กรุณาลองใหม่'));
      const retry = element('button', 'primary', 'ลองอีกครั้ง'); retry.onclick = () => location.reload(); main.append(retry);
      document.body.append(main);
    }
  }
  start();
})();
