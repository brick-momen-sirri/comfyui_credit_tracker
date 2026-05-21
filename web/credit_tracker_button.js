const { app } = window.comfyAPI?.app || {};

const BUTTON_ID = "credit-tracker-toolbar-button";
const BUTTON_CLASS = "credit-tracker-toolbar-button";
const STYLE_ID = "credit-tracker-toolbar-style";

function ensureStyle() {
  if (document.getElementById(STYLE_ID)) {
    return;
  }

  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    #${BUTTON_ID}.${BUTTON_CLASS} {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 36px;
      width: 36px;
      padding: 0;
      border: 0;
      border-radius: 4px;
      background: #4a4a4a;
      color: #f5f5f5;
      font: 700 16px/1 Inter, Segoe UI, Arial, sans-serif;
      cursor: pointer;
      box-shadow: none;
    }
    #${BUTTON_ID}.${BUTTON_CLASS}:hover {
      background: #5a5a5a;
    }
    #${BUTTON_ID}.${BUTTON_CLASS}:active {
      transform: translateY(1px);
    }
    #${BUTTON_ID}.${BUTTON_CLASS} .credit-tracker-icon {
      font-size: 16px;
      line-height: 1;
      opacity: 0.9;
    }
  `;
  document.head.appendChild(style);
}

function buttonText(element) {
  return (element?.innerText || element?.textContent || "").replace(/\s+/g, " ").trim();
}

function normalizedButtonText(element) {
  return buttonText(element).toLowerCase().replace(/[^a-z0-9 ]+/g, "");
}

function isVisible(element) {
  if (!element || !(element instanceof HTMLElement)) {
    return false;
  }
  const rect = element.getBoundingClientRect();
  const style = window.getComputedStyle(element);
  return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
}

function findButtonByText(text, { contains = false } = {}) {
  const wanted = text.toLowerCase().replace(/[^a-z0-9 ]+/g, "");
  return [...document.querySelectorAll("button, .p-button, [role='button']")]
    .filter(isVisible)
    .find((element) => {
      const current = normalizedButtonText(element);
      return contains ? current.includes(wanted) : current === wanted;
    });
}

function findToolbar() {
  const anchor =
    findButtonByText("Monitor", { contains: true }) ||
    findButtonByText("Run") ||
    [...document.querySelectorAll("[data-testid='queue-button'], #queue-button")].find(isVisible);

  if (!anchor) {
    return null;
  }

  const toolbarRow = anchor.closest(".flex.gap-2.mx-2");
  if (toolbarRow && isVisible(toolbarRow)) {
    return { toolbar: toolbarRow, anchor };
  }

  let current = anchor.parentElement;
  let candidate = null;
  while (current && current !== document.body) {
    const rect = current.getBoundingClientRect();
    const visibleButtons = [...current.querySelectorAll("button, .p-button, [role='button']")].filter(isVisible);
    if (rect.height <= 80 && visibleButtons.length >= 2) {
      candidate = { toolbar: current, anchor };
    }
    current = current.parentElement;
  }

  return candidate || { toolbar: anchor.parentElement, anchor };
}

function openCreditTracker() {
  const url = new URL("/credit-tracker", window.location.origin);
  window.open(url.toString(), "_blank", "noopener,noreferrer");
}

function createButton() {
  const button = document.createElement("button");
  button.id = BUTTON_ID;
  button.className = `${BUTTON_CLASS} comfyui-button`;
  button.type = "button";
  button.title = "Open ComfyUI Credit Tracker";
  button.setAttribute("aria-label", "Open ComfyUI Credit Tracker");
  button.innerHTML = `<span class="credit-tracker-icon" aria-hidden="true">◈</span>`;
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openCreditTracker();
  });
  return button;
}

function insertButton() {
  if (document.getElementById(BUTTON_ID)) {
    return true;
  }

  ensureStyle();
  const target = findToolbar();
  if (!target?.toolbar) {
    return false;
  }

  const button = createButton();
  const firstVisibleChild = [...target.toolbar.children].find(isVisible);
  if (firstVisibleChild) {
    target.toolbar.insertBefore(button, firstVisibleChild);
  } else {
    target.toolbar.appendChild(button);
  }
  return true;
}

function installToolbarButton() {
  let attempts = 0;
  const maxAttempts = 80;

  const tryInsert = () => {
    attempts += 1;
    if (insertButton() || attempts >= maxAttempts) {
      return;
    }
    window.setTimeout(tryInsert, 250);
  };

  tryInsert();

  const observer = new MutationObserver(() => {
    if (!document.getElementById(BUTTON_ID)) {
      insertButton();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

if (app?.registerExtension) {
  app.registerExtension({
    name: "ComfyUI.CreditTracker.ToolbarButton",
    setup() {
      installToolbarButton();
    },
  });
} else {
  window.addEventListener("DOMContentLoaded", installToolbarButton, { once: true });
}
