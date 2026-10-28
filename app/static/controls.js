// Keep native select values and change events as the business-data contract.
// Render the interactive surface in the page so OS menus cannot change its size.
const SelectControls = (() => {
  const controls = new WeakMap();
  const popup = document.createElement('div');
  popup.id = 'select-options';
  popup.className = 'select-popup';
  popup.setAttribute('role', 'listbox');
  popup.hidden = true;
  document.body.append(popup);

  let current = null;
  let activeIndex = -1;
  let sequence = 0;
  let search = '';
  let searchTime = 0;
  let positionFrame = 0;

  function nameOf(select) {
    const label = select.labels?.[0];
    const caption = label && [...label.childNodes]
      .filter(node => node.nodeType === Node.TEXT_NODE)
      .map(node => node.textContent).join('').trim();
    return select.getAttribute('aria-label') || caption || '选择选项';
  }

  function available(option) {
    return Boolean(option) && !option.hidden && !option.disabled && !option.closest('optgroup[disabled]');
  }

  function close(restoreFocus = false) {
    if (!current) return;
    const { trigger } = current;
    current = null;
    activeIndex = -1;
    search = '';
    popup.hidden = true;
    popup.replaceChildren();
    trigger.setAttribute('aria-expanded', 'false');
    trigger.removeAttribute('aria-activedescendant');
    if (restoreFocus && trigger.isConnected && !trigger.disabled) trigger.focus();
  }

  function sync(control) {
    const { select, trigger, value } = control;
    value.textContent = select.selectedOptions[0]?.textContent || '请选择';
    trigger.title = value.textContent;
    trigger.disabled = select.disabled || !select.options.length;
    trigger.setAttribute('aria-label', nameOf(select));
    if (select.hasAttribute('aria-describedby')) {
      trigger.setAttribute('aria-describedby', select.getAttribute('aria-describedby'));
    }
    if (current === control && (trigger.disabled || !select.isConnected)) close();
  }

  function position() {
    if (!current) return;
    const rect = current.trigger.getBoundingClientRect();
    if (!rect.width || !rect.height) return close();
    const viewport = window.visualViewport;
    const leftEdge = viewport?.offsetLeft || 0;
    const topEdge = viewport?.offsetTop || 0;
    const width = viewport?.width || document.documentElement.clientWidth;
    const height = viewport?.height || window.innerHeight;
    const rightEdge = leftEdge + width;
    const bottomEdge = topEdge + height;
    if (rect.bottom <= topEdge || rect.top >= bottomEdge || rect.right <= leftEdge || rect.left >= rightEdge) {
      return close();
    }
    const menuWidth = Math.min(Math.max(rect.width, 180), width - 16);
    popup.style.width = `${menuWidth}px`;
    popup.style.maxHeight = '280px';
    const desiredHeight = Math.min(popup.scrollHeight + 2, 280);
    const below = bottomEdge - rect.bottom - 14;
    const above = rect.top - topEdge - 14;
    const upward = below < desiredHeight && above > below;
    popup.style.maxHeight = `${Math.max(36, Math.min(280, upward ? above : below))}px`;
    const menuHeight = popup.getBoundingClientRect().height;
    popup.style.left = `${Math.max(leftEdge + 8, Math.min(rect.left, rightEdge - menuWidth - 8))}px`;
    const top = upward ? rect.top - menuHeight - 6 : rect.bottom + 6;
    popup.style.top = `${Math.max(topEdge + 8, Math.min(top, bottomEdge - menuHeight - 8))}px`;
  }

  function setActive(index) {
    if (!current) return;
    activeIndex = index;
    popup.querySelectorAll('[role=option]').forEach(item => {
      item.classList.toggle('is-active', Number(item.dataset.index) === index);
    });
    const item = popup.querySelector(`[data-index="${index}"]`);
    if (!item) return current.trigger.removeAttribute('aria-activedescendant');
    current.trigger.setAttribute('aria-activedescendant', item.id);
    // Scroll only the menu; scrollIntoView can also move the surrounding form.
    if (item.offsetTop < popup.scrollTop) popup.scrollTop = item.offsetTop;
    if (item.offsetTop + item.offsetHeight > popup.scrollTop + popup.clientHeight) {
      popup.scrollTop = item.offsetTop + item.offsetHeight - popup.clientHeight;
    }
  }

  function open(control) {
    if (control.trigger.disabled) return;
    close();
    current = control;
    searchTime = 0;
    popup.setAttribute('aria-label', nameOf(control.select));
    const fragment = document.createDocumentFragment();
    [...control.select.options].forEach((option, index) => {
      if (option.hidden) return;
      const item = document.createElement('div');
      item.id = `${popup.id}-${index}`;
      item.className = 'select-option';
      item.dataset.index = String(index);
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(option.selected));
      item.setAttribute('aria-disabled', String(!available(option)));
      const text = document.createElement('span');
      text.textContent = option.textContent;
      const check = document.createElement('span');
      check.className = 'select-check';
      check.textContent = option.selected ? '✓' : '';
      check.setAttribute('aria-hidden', 'true');
      item.append(text, check);
      fragment.append(item);
    });
    popup.replaceChildren(fragment);
    popup.hidden = false;
    control.trigger.setAttribute('aria-expanded', 'true');
    position();
    const selected = control.select.selectedIndex;
    setActive(available(control.select.options[selected]) ? selected :
      [...control.select.options].findIndex(available));
  }

  function commit(index) {
    if (!current) return;
    const control = current;
    if (!control.select.options[index] || !available(control.select.options[index])) return;
    const changed = control.select.selectedIndex !== index;
    control.select.selectedIndex = index;
    sync(control);
    close(true);
    if (changed) {
      control.select.dispatchEvent(new Event('input', { bubbles: true }));
      control.select.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  function onKey(control, event) {
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    const { key } = event;
    if (key === 'Tab') return close();
    if (key === 'Escape') {
      if (current) { event.preventDefault(); event.stopPropagation(); close(true); }
      return;
    }
    if (key === 'Enter' || key === ' ') {
      event.preventDefault();
      if (current === control) commit(activeIndex); else open(control);
      return;
    }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(key)) {
      event.preventDefault();
      const wasOpen = current === control;
      if (!wasOpen) open(control);
      const indices = [...control.select.options].flatMap((option, index) => available(option) ? [index] : []);
      if (!indices.length) return;
      let next = indices.indexOf(activeIndex);
      if (key === 'Home') next = 0;
      else if (key === 'End') next = indices.length - 1;
      else if (wasOpen) next = Math.max(0, Math.min(indices.length - 1, next + (key === 'ArrowDown' ? 1 : -1)));
      setActive(indices[Math.max(0, next)]);
      return;
    }
    if (key.length === 1) {
      event.preventDefault();
      if (current !== control) open(control);
      const now = Date.now();
      search = now - searchTime > 700 ? key : search + key;
      searchTime = now;
      const index = [...control.select.options].findIndex(option =>
        available(option) && option.textContent.trim().toLocaleLowerCase().startsWith(search.toLocaleLowerCase()));
      if (index >= 0) setActive(index);
    }
  }

  function enhance(root = document.querySelector('main')) {
    root.querySelectorAll('select:not([multiple])').forEach(select => {
      const existing = controls.get(select);
      if (existing) return sync(existing);
      const wrapper = document.createElement('span');
      wrapper.className = 'select-control';
      if (select.id) wrapper.dataset.selectFor = select.id;
      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.id = `select-trigger-${++sequence}`;
      trigger.className = 'select-trigger';
      trigger.setAttribute('role', 'combobox');
      trigger.setAttribute('aria-haspopup', 'listbox');
      trigger.setAttribute('aria-controls', popup.id);
      trigger.setAttribute('aria-expanded', 'false');
      trigger.setAttribute('aria-autocomplete', 'none');
      const value = document.createElement('span');
      value.className = 'select-value';
      const arrow = document.createElement('span');
      arrow.className = 'select-arrow';
      arrow.setAttribute('aria-hidden', 'true');
      trigger.append(value, arrow);
      wrapper.append(trigger);
      const control = { select, trigger, value };
      controls.set(select, control);
      select.setAttribute('aria-label', nameOf(select));
      [...(select.labels || [])].forEach(label => { label.htmlFor = trigger.id; });
      select.after(wrapper);
      select.hidden = true;
      select.setAttribute('aria-hidden', 'true');
      select.tabIndex = -1;
      trigger.addEventListener('click', () => current === control ? close() : open(control));
      trigger.addEventListener('keydown', event => onKey(control, event));
      trigger.addEventListener('blur', () => { if (current === control) close(); });
      select.addEventListener('change', () => sync(control));
      sync(control);
    });
  }

  popup.addEventListener('mousedown', event => event.preventDefault());
  popup.addEventListener('click', event => {
    const item = event.target.closest('[role=option]');
    if (item) commit(Number(item.dataset.index));
  });
  popup.addEventListener('pointermove', event => {
    const item = event.target.closest('[role=option]');
    if (event.pointerType === 'mouse' && item && item.getAttribute('aria-disabled') !== 'true') {
      setActive(Number(item.dataset.index));
    }
  });
  document.addEventListener('pointerdown', event => {
    if (current && !current.trigger.contains(event.target) && !popup.contains(event.target)) close();
  }, true);
  function schedulePosition() {
    if (!current || positionFrame) return;
    positionFrame = requestAnimationFrame(() => { positionFrame = 0; position(); });
  }
  document.addEventListener('scroll', schedulePosition, true);
  window.addEventListener('resize', schedulePosition);
  window.visualViewport?.addEventListener('resize', schedulePosition);
  window.visualViewport?.addEventListener('scroll', schedulePosition);
  document.addEventListener('reset', () => requestAnimationFrame(() => enhance()));

  return { enhance, close };
})();
