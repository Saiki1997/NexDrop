/* Send to NEXDROP: hands links to the app as nexdrop://add?url=... The app only adds them to its
   links box; nothing downloads until Start is pressed there. Nothing is sent anywhere else. */
"use strict";

const MAX_LINKS = 50;

function appAddress(urls) {
  const params = new URLSearchParams();
  for (const url of urls.slice(0, MAX_LINKS)) params.append("url", url);
  return `nexdrop://add?${params.toString()}`;
}

function webLinks(values) {
  const out = [];
  for (const value of values) {
    const url = String(value || "").trim();
    if (/^https?:\/\//i.test(url) && !out.includes(url)) out.push(url);
  }
  return out;
}

function linksInText(text) {
  return webLinks(String(text || "").match(/https?:\/\/[^\s<>"']+/gi) || []);
}

// Opening a nexdrop:// address hands it to Windows, which starts or focuses NEXDROP.
// The page itself stays where it is.
function send(tab, urls) {
  if (!urls.length) return;
  const address = appAddress(urls);
  if (tab && tab.id !== undefined && tab.id >= 0) chrome.tabs.update(tab.id, { url: address });
  else chrome.tabs.create({ url: address });
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "page", title: "Download this page with NEXDROP", contexts: ["page"] });
    chrome.contextMenus.create({ id: "link", title: "Download link with NEXDROP", contexts: ["link"] });
    chrome.contextMenus.create({ id: "media", title: "Download this file with NEXDROP", contexts: ["image", "video", "audio"] });
    chrome.contextMenus.create({ id: "selection", title: "Download links in the selection with NEXDROP", contexts: ["selection"] });
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "page") send(tab, webLinks([info.pageUrl]));
  else if (info.menuItemId === "link") send(tab, webLinks([info.linkUrl]));
  else if (info.menuItemId === "media") send(tab, webLinks([info.srcUrl]));
  else if (info.menuItemId === "selection") send(tab, linksInText(info.selectionText));
});

chrome.action.onClicked.addListener((tab) => send(tab, webLinks([tab.url])));
