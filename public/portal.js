import { animateClass, initPortalMotion, motionEnabled, playEntryIntro } from './portal-motion.js?v=1';

const get = id => document.getElementById(id);
const layout = document.querySelector('.portal-layout');
const card = document.querySelector('.auth-card');
const form = get('auth-form');
const fields = {
  name: get('full-name'),
  grade: get('student-grade'),
  school: get('student-school'),
  guardian_name: get('guardian-name'),
  guardian_phone: get('guardian-phone'),
  guardian_relationship: get('guardian-relationship'),
};
const identifier = get('email');
const password = get('password');
const passwordToggle = get('password-toggle');
const submit = get('enter-demo');
const feedback = get('auth-feedback');
const entryLabel = get('entry-label');
const tabs = [get('login-tab'), get('register-tab')];
const subjectDisplay = document.querySelector('.subject-display');
const subjectButtons = [...document.querySelectorAll('[data-subject]')];

const subjects = {
  physics: ['ฟิสิกส์', 'เข้าใจแรง การเคลื่อนที่ และพลังงานผ่านโจทย์ที่เห็นภาพ', 'F = ma'],
  chemistry: ['เคมี', 'เชื่อมสูตรกับภาพของสารและปฏิกิริยา เพื่อแก้โจทย์อย่างมั่นใจ', 'H₂O'],
  biology: ['ชีวะ', 'มองเห็นความสัมพันธ์ของเซลล์ ระบบร่างกาย และสิ่งมีชีวิต', 'DNA'],
  math: ['คณิตศาสตร์', 'เข้าใจวิธีคิดทีละขั้น ก่อนลงมือฝึกโจทย์ด้วยตัวเอง', '∫ x dx'],
  general: ['ภาษาอังกฤษ', 'ฝึกอ่าน เขียน และสื่อสารอย่างมั่นใจ พร้อมใช้สอบในระดับมัธยม', 'A → Z'],
};

const errorMessages = {
  invalid_credentials: 'อีเมล ชื่อผู้ใช้ หรือรหัสผ่านไม่ถูกต้อง',
  account_exists: 'อีเมลนี้มีบัญชีแล้ว กรุณาเข้าสู่ระบบ',
  weak_password: 'กรุณาตั้งรหัสผ่าน 12–128 ตัวอักษร',
  rate_limited: 'ลองหลายครั้งเกินไป กรุณารอสักครู่แล้วลองใหม่',
};

let registration = false;
let submitting = false;
let existingSession;
let authAnimation = 0;
let subjectAnimation = 0;

function setPasswordVisibility(visible) {
  password.type = visible ? 'text' : 'password';
  passwordToggle.setAttribute('aria-pressed', String(visible));
  passwordToggle.setAttribute('aria-label', visible ? 'ซ่อนรหัสผ่าน' : 'แสดงรหัสผ่าน');
}

function showAuthMotion(name, x, y) {
  card.classList.remove('auth-switch', 'auth-pulse');
  card.style.setProperty('--wave-x', x);
  card.style.setProperty('--wave-y', y);
  authAnimation = animateClass(card, name, authAnimation);
}

function setMode(register, { animate = true } = {}) {
  const changed = registration !== register;
  registration = register;
  layout.classList.toggle('is-register', register);
  tabs.forEach((tab, index) => {
    const selected = index === Number(register);
    tab.classList.toggle('selected', selected);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });

  get('auth-panel').setAttribute('aria-labelledby', tabs[Number(register)].id);
  get('auth-title').textContent = register ? 'สมัครสมาชิกนักเรียน' : 'เข้าสู่ระบบ';
  get('auth-subtitle').textContent = register
    ? 'กรอกข้อมูลนักเรียนและผู้ปกครองเพื่อสร้างบัญชี'
    : 'นักเรียน ครู และผู้ดูแลใช้หน้าเข้าสู่ระบบเดียวกัน';
  get('name-row').hidden = !register;
  get('profile-fields').hidden = !register;
  Object.values(fields).forEach(field => { field.disabled = !register; });
  identifier.type = register ? 'email' : 'text';
  identifier.autocomplete = register ? 'email' : 'username';
  get('identifier-label').textContent = register ? 'อีเมลนักเรียน' : 'อีเมลหรือชื่อผู้ใช้';
  get('password-hint').hidden = !register;
  password.autocomplete = register ? 'new-password' : 'current-password';
  setPasswordVisibility(false);
  entryLabel.textContent = register ? 'สร้างบัญชีนักเรียน' : 'เข้าสู่ระบบ';
  form.action = register ? '/api/auth/register' : '/api/auth/login';

  if (changed) {
    feedback.textContent = '';
    if (animate) showAuthMotion('auth-switch', register ? '70%' : '30%', '30%');
  }
}

async function checkExistingSession() {
  const controller = new AbortController();
  existingSession = controller;
  try {
    await new Promise(resolve => requestAnimationFrame(resolve));
    if (controller.signal.aborted) return;
    const response = await fetch('/api/me', {
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!response.ok) return;
    const state = await response.json();
    if (state.authenticated === true && !controller.signal.aborted) {
      window.location.replace('/dashboard.html');
    }
  } catch {
    // The optional session lookup never prevents manual sign-in.
  } finally {
    if (existingSession === controller) existingSession = undefined;
  }
}

function getSubmission() {
  const login = identifier.value.trim();
  if (registration) {
    const missing = Object.values(fields).find(field => !field.value.trim());
    if (missing) return { field: missing, message: 'กรุณากรอกข้อมูลนักเรียนและผู้ปกครองให้ครบ' };
  }
  if (!login) return { field: identifier, message: registration ? 'กรุณากรอกอีเมลนักเรียน' : 'กรุณากรอกอีเมลหรือชื่อผู้ใช้' };
  if (registration && !identifier.checkValidity()) return { field: identifier, message: 'กรุณากรอกอีเมลที่ถูกต้อง' };
  if (!password.value) return { field: password, message: 'กรุณากรอกรหัสผ่าน' };
  const passwordLength = Array.from(password.value).length;
  if (registration && (passwordLength < 12 || passwordLength > 128)) {
    return { field: password, message: errorMessages.weak_password };
  }

  const credentials = { password: password.value };
  if (!registration) return { payload: { login, ...credentials } };
  const profile = Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.value.trim()]));
  return { payload: { ...profile, email: login, ...credentials } };
}

function setSubmitting(state) {
  submitting = state;
  submit.disabled = state;
  form.setAttribute('aria-busy', String(state));
  tabs.forEach(tab => { tab.disabled = state; });
  card.classList.toggle('is-submitting', state);
  entryLabel.textContent = registration
    ? (state ? 'กำลังสร้างบัญชี…' : 'สร้างบัญชีนักเรียน')
    : (state ? 'กำลังเข้าสู่ระบบ…' : 'เข้าสู่ระบบ');
}

async function submitAuth(event) {
  event.preventDefault();
  if (submitting) return;
  const { payload, field, message } = getSubmission();
  if (message) {
    feedback.textContent = message;
    field.focus();
    return;
  }

  existingSession?.abort();
  setPasswordVisibility(false);
  setSubmitting(true);
  feedback.textContent = entryLabel.textContent;
  showAuthMotion('auth-pulse', '50%', '76%');
  let leaving = false;

  try {
    const response = await fetch(registration ? '/api/auth/register' : '/api/auth/login', {
      method: 'POST',
      mode: 'same-origin',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok || result.authenticated !== true) {
      const code = result.error?.code;
      feedback.textContent = Object.hasOwn(errorMessages, code)
        ? errorMessages[code]
        : (registration ? 'สร้างบัญชีไม่สำเร็จ กรุณาตรวจข้อมูลแล้วลองอีกครั้ง' : 'เข้าสู่ระบบไม่สำเร็จ กรุณาลองอีกครั้ง');
      return;
    }

    card.classList.remove('is-submitting');
    card.classList.add('is-confirmed');
    password.value = '';
    entryLabel.textContent = 'สำเร็จ';
    feedback.textContent = registration ? 'สร้างบัญชีสำเร็จ' : 'เข้าสู่ระบบสำเร็จ';

    // Give the success state a paint without waiting for the animation to end.
    if (motionEnabled()) {
      await new Promise(resolve => {
        const fallback = setTimeout(resolve, 60);
        requestAnimationFrame(() => requestAnimationFrame(() => {
          clearTimeout(fallback);
          resolve();
        }));
      });
    }
    window.location.assign('/dashboard.html');
    leaving = true;
  } catch {
    feedback.textContent = 'ติดต่อเซิร์ฟเวอร์ไม่ได้ กรุณาลองอีกครั้ง';
  } finally {
    if (!leaving) {
      card.classList.remove('is-confirmed');
      setSubmitting(false);
    }
  }
}

function chooseMode(index) {
  if (submitting) return;
  existingSession?.abort();
  setMode(index === 1);
}

tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => chooseMode(index));
  tab.addEventListener('keydown', event => {
    const keys = { ArrowLeft: 1 - index, ArrowRight: 1 - index, Home: 0, End: 1 };
    if (!Object.hasOwn(keys, event.key) || submitting) return;
    event.preventDefault();
    chooseMode(keys[event.key]);
    tabs[keys[event.key]].focus();
  });
});

passwordToggle.addEventListener('click', () => setPasswordVisibility(password.type === 'password'));
form.addEventListener('submit', submitAuth);
form.addEventListener('input', () => existingSession?.abort());
get('about-button').addEventListener('click', () => get('info').showModal());

subjectButtons.forEach(button => {
  button.addEventListener('click', () => {
    const [title, description, formula] = subjects[button.dataset.subject];
    for (const subjectButton of subjectButtons) {
      const active = subjectButton === button;
      subjectButton.classList.toggle('active', active);
      subjectButton.setAttribute('aria-pressed', String(active));
    }
    get('subject-title').textContent = title;
    get('subject-description').textContent = description;
    get('subject-formula').textContent = formula;
    subjectAnimation = animateClass(subjectDisplay, 'subject-switch', subjectAnimation);
  });
});

get('portal-fonts').media = 'all';
setMode(false, { animate: false });
identifier.disabled = false;
password.disabled = false;
submit.disabled = false;
initPortalMotion({ card, layout, subjectDisplay });
playEntryIntro(get('entry-intro'));
checkExistingSession();

window.addEventListener('pageshow', event => {
  if (!event.persisted) return;
  card.classList.remove('is-confirmed', 'auth-switch', 'auth-pulse');
  setSubmitting(false);
  feedback.textContent = '';
  checkExistingSession();
});
