const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)');

export function motionEnabled() {
  return !reducedMotion.matches && !document.documentElement.classList.contains('motion-paused');
}

export function playEntryIntro(intro) {
  if (!motionEnabled() || document.hidden || document.activeElement !== document.body) return;
  if (performance.getEntriesByType('navigation')[0]?.type === 'back_forward') return;

  try {
    if (sessionStorage.getItem('physics-plus-entry-seen')) return;
    sessionStorage.setItem('physics-plus-entry-seen', '1');
  } catch {
    // The intro preference is optional when browser storage is unavailable.
  }

  const events = new AbortController();
  const finish = () => {
    clearTimeout(fallback);
    events.abort();
    intro.hidden = true;
    intro.classList.remove('is-playing');
  };
  const fallback = setTimeout(finish, 1250);
  const options = { signal: events.signal };

  intro.addEventListener('animationend', event => {
    if (event.target === intro && event.animationName === 'entry-intro-exit') finish();
  }, options);
  reducedMotion.addEventListener('change', finish, options);
  document.addEventListener('visibilitychange', finish, options);
  for (const event of ['pointerdown', 'keydown', 'focusin', 'wheel', 'touchstart']) {
    window.addEventListener(event, finish, { ...options, capture: true, passive: true });
  }

  intro.hidden = false;
  intro.classList.add('is-playing');
}

export function animateClass(element, className, previousFrame) {
  cancelAnimationFrame(previousFrame);
  element.classList.remove(className);
  if (!motionEnabled()) return 0;
  return requestAnimationFrame(() => {
    if (motionEnabled()) element.classList.add(className);
  });
}

function bindPointerGlow(card) {
  let pendingFrame = 0;
  let point;

  function clearGlow() {
    cancelAnimationFrame(pendingFrame);
    pendingFrame = 0;
    card.style.removeProperty('--pointer-x');
    card.style.removeProperty('--pointer-y');
  }

  card.addEventListener('pointermove', event => {
    if (!finePointer.matches || !motionEnabled()) return;
    point = { x: event.clientX, y: event.clientY };
    if (pendingFrame) return;
    pendingFrame = requestAnimationFrame(() => {
      pendingFrame = 0;
      const bounds = card.getBoundingClientRect();
      card.style.setProperty('--pointer-x', `${point.x - bounds.left}px`);
      card.style.setProperty('--pointer-y', `${point.y - bounds.top}px`);
    });
  }, { passive: true });
  card.addEventListener('pointerleave', clearGlow);
  reducedMotion.addEventListener('change', clearGlow);
  return clearGlow;
}

export function initPortalMotion({ card, layout, subjectDisplay }) {
  const clearGlow = bindPointerGlow(card);
  const button = document.getElementById('motion-control');
  button.addEventListener('click', () => {
    const paused = document.documentElement.classList.toggle('motion-paused');
    const label = paused ? 'เริ่มภาพเคลื่อนไหว' : 'หยุดภาพเคลื่อนไหว';
    button.setAttribute('aria-pressed', String(paused));
    button.setAttribute('aria-label', label);
    button.querySelector('.motion-icon').textContent = paused ? '▶' : 'Ⅱ';
    button.querySelector('.motion-label').textContent = label;

    if (paused) {
      clearGlow();
      card.classList.remove('auth-switch', 'auth-pulse');
      subjectDisplay.classList.remove('subject-switch');
    }
  });

  const progress = document.querySelector('.page-progress');
  const backToTop = document.getElementById('back-to-top');
  let pendingFrame = 0;
  function updateScrollEffects() {
    pendingFrame = 0;
    const distance = document.documentElement.scrollHeight - window.innerHeight;
    const fraction = distance > 0 ? Math.min(1, Math.max(0, window.scrollY / distance)) : 0;
    progress.style.transform = `scaleX(${fraction})`;
    backToTop.hidden = window.scrollY < Math.min(window.innerHeight * .7, 360);
  }

  function requestScrollUpdate() {
    if (!pendingFrame) pendingFrame = requestAnimationFrame(updateScrollEffects);
  }

  window.addEventListener('scroll', requestScrollUpdate, { passive: true });
  window.addEventListener('resize', requestScrollUpdate);
  if ('ResizeObserver' in window) new ResizeObserver(requestScrollUpdate).observe(layout);
  backToTop.addEventListener('click', () => {
    document.querySelector('.brand').focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: motionEnabled() ? 'smooth' : 'instant' });
  });
  updateScrollEffects();
}
