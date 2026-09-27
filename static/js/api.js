const api = {
  async request(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    });
    if (res.status === 401) {
      location.hash = "#/login";
      throw new Error("未登录");
    }
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || "请求失败");
    }
    return res.json();
  },
  get(url) { return this.request("GET", url); },
  post(url, body) { return this.request("POST", url, body); },
  put(url, body) { return this.request("PUT", url, body); },
};

const fmtNum = (v, digits = 2) =>
  v === null || v === undefined ? "-" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });

const StatusBadge = (s) => {
  const map = {
    active: ["active", "正常"],
    inactive: ["muted", "停用"],
    pending: ["warn", "待处理"],
    allocated: ["info", "已分配"],
    cleared: ["ok", "已清缴"],
    compliant: ["ok", "履约达标"],
    deficit: ["danger", "配额缺口"],
    draft: ["muted", "草稿"],
    submitted: ["info", "已提交"],
    approved: ["ok", "已批准"],
  };
  const [cls, label] = map[s] || ["muted", s];
  return `<span class="badge ${cls}">${label}</span>`;
};

const scopeLabel = (s) => ({ 1: "范围一", 2: "范围二", 3: "范围三" }[s] || s);
const txLabel = {
  allocation: "配额分配",
  buy: "买入",
  sell: "卖出",
  transfer_in: "转入",
  transfer_out: "转出",
  offset: "抵消",
  clear: "履约清缴",
};
