from __future__ import annotations

from typing import Any


INTERACTION_PROBE = r"""
(() => {
  if (window.__IK_INTERACTION_PROBE_INSTALLED) return;
  window.__IK_INTERACTION_PROBE_INSTALLED = true;
  window.__IK_SYNC_SOURCE = false;
  window.__IK_INSPECT_ENABLED = false;
  window.__IK_DRAG_ITEM_VISIBLE = true;
  window.__IK_SYNC_EVENTS = [];
  window.__IK_COORDINATE_EVENTS = [];
  let pointerActive = false;
  let lastMoveAt = 0;
  let sequence = 0;
  const activeKeys = new Map();

  const round = (value, digits = 6) => {
    const factor = 10 ** digits;
    return Math.round(value * factor) / factor;
  };

  const visibleCanvasAt = (x, y) => {
    const candidates = [...document.querySelectorAll('canvas')]
      .map((canvas, index) => ({canvas, index, rect: canvas.getBoundingClientRect()}))
      .filter(({rect}) => rect.width > 0 && rect.height > 0 &&
        x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom)
      .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
    return candidates[0] || null;
  };

  const selectorFor = (element) => {
    if (!(element instanceof Element)) return '';
    if (element.id) return `#${CSS.escape(element.id)}`;
    const parts = [];
    let current = element;
    while (current && current.nodeType === 1 && parts.length < 5) {
      let part = current.tagName.toLowerCase();
      if (current.classList.length) {
        part += '.' + [...current.classList].slice(0, 2).map((name) => CSS.escape(name)).join('.');
      }
      const parent = current.parentElement;
      if (parent) {
        const siblings = [...parent.children].filter((item) => item.tagName === current.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
      }
      parts.unshift(part);
      current = parent;
    }
    return parts.join(' > ');
  };

  const describe = (event, type) => {
    const x = Number(event.clientX || 0);
    const y = Number(event.clientY || 0);
    const viewportWidth = Math.max(1, window.innerWidth);
    const viewportHeight = Math.max(1, window.innerHeight);
    const element = document.elementFromPoint(x, y) || event.target;
    const elementRect = element instanceof Element ? element.getBoundingClientRect() : null;
    const found = visibleCanvasAt(x, y);
    let canvas = null;
    if (found) {
      const cssX = x - found.rect.left;
      const cssY = y - found.rect.top;
      const ratioX = cssX / found.rect.width;
      const ratioY = cssY / found.rect.height;
      const pixelX = ratioX * found.canvas.width;
      const pixelY = ratioY * found.canvas.height;
      canvas = {
        index: found.index,
        css_x: round(cssX, 3),
        css_y: round(cssY, 3),
        css_width: round(found.rect.width, 3),
        css_height: round(found.rect.height, 3),
        ratio_x: round(ratioX),
        ratio_y: round(ratioY),
        pixel_x: round(pixelX, 3),
        pixel_y: round(pixelY, 3),
        pixel_x_rounded: Math.round(pixelX),
        pixel_y_rounded: Math.round(pixelY),
        backing_width: found.canvas.width,
        backing_height: found.canvas.height
      };
    }
    return {
      sequence: ++sequence,
      type,
      captured_at: new Date().toISOString(),
      frame_url: location.href,
      viewport: {
        x: round(x, 3),
        y: round(y, 3),
        width: viewportWidth,
        height: viewportHeight,
        ratio_x: round(x / viewportWidth),
        ratio_y: round(y / viewportHeight)
      },
      screen: {
        x: Number(event.screenX || 0),
        y: Number(event.screenY || 0),
        device_pixel_ratio: window.devicePixelRatio || 1
      },
      canvas,
      element: {
        tag: element?.tagName?.toLowerCase() || '',
        id: element?.id || '',
        class_name: typeof element?.className === 'string' ? element.className.slice(0, 200) : '',
        selector: selectorFor(element),
        text: (element?.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 160),
        box: elementRect ? {
          x: round(elementRect.x, 3),
          y: round(elementRect.y, 3),
          width: round(elementRect.width, 3),
          height: round(elementRect.height, 3)
        } : null
      },
      pointer: {
        button: Number(event.button ?? 0),
        buttons: Number(event.buttons ?? 0),
        pointer_type: event.pointerType || 'mouse'
      }
    };
  };

  const pushSync = (event, type) => {
    if (!window.__IK_SYNC_SOURCE || window.__IK_INSPECT_ENABLED) return;
    window.__IK_SYNC_EVENTS.push(describe(event, type));
    if (window.__IK_SYNC_EVENTS.length > 500) window.__IK_SYNC_EVENTS.splice(0, 100);
  };

  const describeKeyboard = (event, type) => ({
    sequence: ++sequence,
    type,
    captured_at: new Date().toISOString(),
    frame_url: location.href,
    keyboard: {
      key: String(event.key || ''),
      code: String(event.code || ''),
      key_code: Number(event.keyCode || event.which || 0),
      location: Number(event.location || 0),
      repeat: Boolean(event.repeat),
      is_composing: Boolean(event.isComposing),
      alt: Boolean(event.altKey),
      ctrl: Boolean(event.ctrlKey),
      meta: Boolean(event.metaKey),
      shift: Boolean(event.shiftKey)
    }
  });

  const pushKeyboardSync = (event, type) => {
    if (!window.__IK_SYNC_SOURCE || window.__IK_INSPECT_ENABLED || event.isComposing) return;
    const row = describeKeyboard(event, type);
    window.__IK_SYNC_EVENTS.push(row);
    if (window.__IK_SYNC_EVENTS.length > 500) window.__IK_SYNC_EVENTS.splice(0, 100);
    const identity = row.keyboard.code || row.keyboard.key;
    if (type === 'keydown') activeKeys.set(identity, row);
    else activeKeys.delete(identity);
  };

  const blockInspector = (event, record = false) => {
    if (!window.__IK_INSPECT_ENABLED) return false;
    if (record) {
      window.__IK_COORDINATE_EVENTS.push(describe(event, 'coordinate'));
      if (window.__IK_COORDINATE_EVENTS.length > 100) window.__IK_COORDINATE_EVENTS.shift();
    }
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    return true;
  };

  window.addEventListener('pointerdown', (event) => {
    if (blockInspector(event, true)) return;
    pointerActive = true;
    pushSync(event, 'pointerdown');
  }, true);
  window.addEventListener('pointermove', (event) => {
    if (window.__IK_INSPECT_ENABLED) return;
    if (!pointerActive || !window.__IK_SYNC_SOURCE) return;
    const now = performance.now();
    if (now - lastMoveAt < 24) return;
    lastMoveAt = now;
    pushSync(event, 'pointermove');
  }, true);
  window.addEventListener('pointerup', (event) => {
    if (blockInspector(event, false)) return;
    pushSync(event, 'pointerup');
    pointerActive = false;
  }, true);
  window.addEventListener('pointercancel', (event) => {
    if (blockInspector(event, false)) return;
    pushSync(event, 'pointerup');
    pointerActive = false;
  }, true);
  window.addEventListener('click', (event) => blockInspector(event, false), true);
  window.addEventListener('contextmenu', (event) => blockInspector(event, false), true);
  window.addEventListener('wheel', (event) => {
    if (blockInspector(event, false)) return;
    if (!window.__IK_SYNC_SOURCE) return;
    const row = describe(event, 'wheel');
    row.wheel = {delta_x: event.deltaX, delta_y: event.deltaY, delta_mode: event.deltaMode};
    window.__IK_SYNC_EVENTS.push(row);
  }, {capture: true, passive: false});
  window.addEventListener('keydown', (event) => pushKeyboardSync(event, 'keydown'), true);
  window.addEventListener('keyup', (event) => pushKeyboardSync(event, 'keyup'), true);
  window.addEventListener('blur', () => {
    if (!window.__IK_SYNC_SOURCE || window.__IK_INSPECT_ENABLED) return;
    for (const row of activeKeys.values()) {
      window.__IK_SYNC_EVENTS.push({...row, sequence: ++sequence, type: 'keyup'});
    }
    activeKeys.clear();
  }, true);

  window.__IK_SET_INTERACTION_MODES = (syncSource, inspectEnabled) => {
    window.__IK_SYNC_SOURCE = Boolean(syncSource);
    window.__IK_INSPECT_ENABLED = Boolean(inspectEnabled);
    let banner = document.getElementById('ik-coordinate-inspector-banner');
    if (window.__IK_INSPECT_ENABLED && document.body) {
      if (!banner) {
        banner = document.createElement('div');
        banner.id = 'ik-coordinate-inspector-banner';
        banner.textContent = 'ĐO TỌA ĐỘ — click vào nút/vị trí cần lấy';
        Object.assign(banner.style, {
          position: 'fixed', top: '8px', left: '50%', transform: 'translateX(-50%)',
          zIndex: '2147483647', background: 'rgba(180, 0, 0, .92)', color: '#fff',
          padding: '7px 14px', borderRadius: '5px', font: 'bold 13px Segoe UI, sans-serif',
          pointerEvents: 'none', boxShadow: '0 2px 8px rgba(0,0,0,.35)'
        });
        document.body.appendChild(banner);
      }
    } else if (banner) {
      banner.remove();
    }
  };

  window.__IK_SET_DRAG_ITEM_VISIBLE = (visible) => {
    window.__IK_DRAG_ITEM_VISIBLE = Boolean(visible);
    const styleId = 'ik-auto-drag-item-visibility';
    let style = document.getElementById(styleId);
    if (window.__IK_DRAG_ITEM_VISIBLE) {
      if (style) style.remove();
      return;
    }
    if (!style && document.documentElement) {
      style = document.createElement('style');
      style.id = styleId;
      style.textContent = '#drag-item { display: none !important; visibility: hidden !important; pointer-events: none !important; }';
      (document.head || document.documentElement).appendChild(style);
    }
  };

})();
"""


def validate_viewport(width: int, height: int) -> tuple[int, int]:
    if not 320 <= width <= 7680:
        raise ValueError("Chiều rộng phải trong khoảng 320..7680 px")
    if not 240 <= height <= 4320:
        raise ValueError("Chiều cao phải trong khoảng 240..4320 px")
    return width, height


def calculate_target_point(event: dict[str, Any], box: dict[str, float]) -> tuple[float, float]:
    """Map a source gesture to a follower's live page canvas rectangle.

    Source CSS/backing dimensions are intentionally ignored. The probe has
    already reduced the master point to ratios, so maximizing the master (or
    restoring it) cannot change the logical point sent to compact followers.
    Only the follower's current rectangle participates in this transform. CDP
    mouse events are page-viewport coordinates, not iframe-local coordinates,
    so the canvas' measured x/y must be included exactly once.
    """
    canvas = event.get("canvas")
    if isinstance(canvas, dict):
        ratio_x = float(canvas.get("ratio_x", 0.0))
        ratio_y = float(canvas.get("ratio_y", 0.0))
    else:
        viewport = event.get("viewport", {})
        ratio_x = float(viewport.get("ratio_x", 0.0))
        ratio_y = float(viewport.get("ratio_y", 0.0))
    ratio_x = min(1.0, max(0.0, ratio_x))
    ratio_y = min(1.0, max(0.0, ratio_y))
    return (
        float(box.get("x", 0.0)) + float(box["width"]) * ratio_x,
        float(box.get("y", 0.0)) + float(box["height"]) * ratio_y,
    )


def format_coordinate(profile_id: str, event: dict[str, Any]) -> str:
    canvas = event.get("canvas")
    if isinstance(canvas, dict):
        return (
            f"{profile_id} | canvas px=({canvas.get('pixel_x_rounded')}, "
            f"{canvas.get('pixel_y_rounded')}) | css=({canvas.get('css_x')}, "
            f"{canvas.get('css_y')}) | size={canvas.get('backing_width')}x"
            f"{canvas.get('backing_height')}"
        )
    viewport = event.get("viewport", {})
    return (
        f"{profile_id} | viewport=({viewport.get('x')}, {viewport.get('y')}) | "
        f"size={viewport.get('width')}x{viewport.get('height')}"
    )
