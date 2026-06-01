import { app } from "../../scripts/app.js";

const BUTTON_ID = "credit-tracker-toolbar-button";
const BUTTON_CLASS = "credit-tracker-toolbar-button";
const STYLE_ID = "credit-tracker-toolbar-style";
const FLOATING_CLASS = "credit-tracker-floating";
const TOOLBAR_CLASS = "credit-tracker-in-toolbar";

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
      min-width: 36px;
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
      display: block;
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: #f5f5f5;
      box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.14), 0 0 10px rgba(255, 255, 255, 0.36);
      opacity: 0.95;
    }
    #${BUTTON_ID}.${FLOATING_CLASS} {
      position: fixed;
      top: calc(var(--comfy-topbar-height, 48px) + 12px);
      right: 18px;
      z-index: 10000;
    }
    #${BUTTON_ID}.${TOOLBAR_CLASS} {
      position: static;
      flex: 0 0 auto;
      margin: 0;
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

function findButtonByAttributes(patterns) {
  return [...document.querySelectorAll("button, .p-button, [role='button']")]
    .filter(isVisible)
    .find((element) => {
      const values = [
        element.id,
        element.getAttribute("data-testid"),
        element.getAttribute("aria-label"),
        element.getAttribute("title"),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();

      return patterns.some((pattern) => values.includes(pattern));
    });
}

function directChildOf(parent, child) {
  let current = child;
  while (current?.parentElement && current.parentElement !== parent) {
    current = current.parentElement;
  }
  return current?.parentElement === parent ? current : child;
}

function findToolbar() {
  const anchor =
    findButtonByAttributes(["queue", "run", "monitor"]) ||
    findButtonByText("Monitor", { contains: true }) ||
    findButtonByText("Queue", { contains: true }) ||
    findButtonByText("Run") ||
    [...document.querySelectorAll("[data-testid='queue-button'], #queue-button")].find(isVisible);

  if (!anchor) {
    return null;
  }

  const toolbarRow = anchor.closest(
    ".flex.gap-2.mx-2, .flex.items-center, .p-toolbar, [role='toolbar'], [data-testid*='toolbar']"
  );
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
  button.innerHTML = `<span class="credit-tracker-icon" aria-hidden="true"></span>`;
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openCreditTracker();
  });
  return button;
}

function insertButton() {
  ensureStyle();

  const button = document.getElementById(BUTTON_ID) || createButton();
  const target = findToolbar();

  if (target?.toolbar && target.anchor) {
    const anchorChild = directChildOf(target.toolbar, target.anchor);
    button.classList.remove(FLOATING_CLASS);
    button.classList.add(TOOLBAR_CLASS);
    anchorChild.after(button);
    return true;
  }

  if (!button.isConnected) {
    button.classList.remove(TOOLBAR_CLASS);
    button.classList.add(FLOATING_CLASS);
    document.body.appendChild(button);
  }

  return false;
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
    const button = document.getElementById(BUTTON_ID);
    if (!button || !button.isConnected || button.classList.contains(FLOATING_CLASS)) {
      insertButton();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

app.registerExtension({
  name: "ComfyUI.CreditTracker.ToolbarButton",
  setup() {
    installToolbarButton();
  },
});
