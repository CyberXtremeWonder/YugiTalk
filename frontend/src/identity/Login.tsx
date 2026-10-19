import { useEffect, useState, type FormEvent } from "react";
import { identityRequest, type Identity } from "./api";

/** 展示登录表单，验证输入并在成功后更新工作台身份。 */
export function Login({
  onLogin,
  initialError = "",
}: {
  onLogin: (data: Identity) => void;
  initialError?: string;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(initialError);
  const [busy, setBusy] = useState(false);
  const [registering, setRegistering] = useState(false);
  const [development, setDevelopment] = useState(false);
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [sending, setSending] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [notice, setNotice] = useState("");
  const [options, setOptions] = useState<{
    registration_enabled: boolean;
    mail_ready: boolean;
  } | null>(null);
  useEffect(() => {
    document.documentElement.dataset.theme = "light";
    let active = true;
    void identityRequest<{
      registration_enabled: boolean;
      mail_ready: boolean;
    }>("/auth/options")
      .then((value) => {
        if (active) setOptions(value);
      })
      .catch(() => {
        if (active) setNotice("暂时无法确认注册服务状态，登录仍可使用。");
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    if (!cooldown) return;
    const timer = window.setTimeout(
      () => setCooldown((value) => Math.max(0, value - 1)),
      1000,
    );
    return () => window.clearTimeout(timer);
  }, [cooldown]);
  /** 发送验证码并限制重发，页面不接收或显示验证码原文。 */
  async function sendCode() {
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())) {
      setError("请填写有效邮箱。");
      return;
    }
    setSending(true);
    setError("");
    setNotice("");
    try {
      await identityRequest("/auth/registration-code", "POST", { email });
      setCooldown(60);
      setNotice("如果该邮箱可以注册，验证码将发送到邮箱，请检查收件箱。");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "验证码发送失败");
    } finally {
      setSending(false);
    }
  }
  /** 切换登录或注册，清除凭证与旧验证码，避免跨模式提交。 */
  function switchForm(value: boolean) {
    setRegistering(value);
    setDevelopment(false);
    setPassword("");
    setConfirmation("");
    setCode("");
    setError("");
    setNotice("");
  }
  /** 提交登录凭证，限制重复提交并显示可公开的错误提示。 */
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (registering && password !== confirmation) {
      setError("两次密码不一致。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (registering) {
        await identityRequest("/auth/register", "POST", {
          email,
          name,
          password,
          code,
        });
        switchForm(false);
        setNotice("注册成功，请登录。");
        return;
      }
      const result = await identityRequest<Identity>("/auth/login", "POST", {
        email,
        password,
        development_mode: development,
      });
      setPassword("");
      onLogin(result);
    } catch (failure) {
      setPassword("");
      setError(failure instanceof Error ? failure.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <a className="brand" href="#chat" aria-label="YugiTalk 首页">
        <span className="brand-mark" aria-hidden="true">
          Y↗
        </span>
        yugitalk<span className="brand-dot">.</span>
      </a>
      <div className="login-card">
        <span className="login-kicker">YOUR IDEAS, IN ACTION.</span>
        <h1>{registering ? "创建你的账号。" : "欢迎回来。"}</h1>
        <p>
          {registering
            ? "验证邮箱，开始你的知识与行动空间。"
            : "登录你的知识与行动空间。"}
        </p>
        <div className="auth-tabs" aria-label="账号入口">
          <button
            type="button"
            aria-label="切换到登录"
            aria-pressed={!registering}
            disabled={busy || sending}
            onClick={() => switchForm(false)}
          >
            登录
          </button>
          <button
            type="button"
            aria-label="切换到注册"
            aria-pressed={registering}
            disabled={busy || sending}
            onClick={() => switchForm(true)}
          >
            注册
          </button>
        </div>
        <form onSubmit={(event) => void submit(event)}>
          <label htmlFor="email">邮箱</label>
          <input
            id="email"
            aria-label="邮箱"
            placeholder={registering ? "填写你的邮箱" : "邮箱"}
            type={registering ? "email" : "text"}
            autoComplete="username"
            maxLength={254}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
            disabled={busy || sending}
          />
          {registering && (
            <>
              <label htmlFor="register-name">显示名称</label>
              <input
                id="register-name"
                value={name}
                maxLength={80}
                onChange={(event) => setName(event.target.value)}
                required
                disabled={busy || sending}
                autoComplete="nickname"
              />
              <label htmlFor="verification-code">邮箱验证码</label>
              <div className="verification-row">
                <input
                  id="verification-code"
                  value={code}
                  onChange={(event) =>
                    setCode(event.target.value.replace(/\D/g, "").slice(0, 6))
                  }
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  pattern="[0-9]{6}"
                  maxLength={6}
                  required
                  disabled={busy || sending}
                />
                <button
                  type="button"
                  onClick={() => void sendCode()}
                  disabled={
                    busy ||
                    sending ||
                    cooldown > 0 ||
                    !options?.registration_enabled ||
                    !options.mail_ready
                  }
                >
                  {sending
                    ? "发送中…"
                    : cooldown
                      ? `${cooldown}秒后重发`
                      : "获取验证码"}
                </button>
              </div>
            </>
          )}
          <label htmlFor="password">密码</label>
          <input
            id="password"
            type="password"
            autoComplete={registering ? "new-password" : "current-password"}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
            disabled={busy || sending}
          />
          {registering ? (
            <>
              <label htmlFor="confirm-password">确认密码</label>
              <input
                id="confirm-password"
                type="password"
                value={confirmation}
                autoComplete="new-password"
                onChange={(event) => setConfirmation(event.target.value)}
                required
                disabled={busy || sending}
              />
              {!options?.mail_ready && (
                <p className="login-note">
                  邮件服务尚未配置，暂时无法完成邮箱验证。
                </p>
              )}
              {options && !options.registration_enabled && (
                <p className="login-note">当前服务未开放注册。</p>
              )}
            </>
          ) : (
            <label className="development-choice">
              <input
                type="checkbox"
                checked={development}
                onChange={(event) => setDevelopment(event.target.checked)}
                disabled={busy}
              />
              开发模式（仅管理员）
            </label>
          )}
          {error && <p role="alert">{error}</p>}
          {notice && <p role="status">{notice}</p>}
          <button
            type="submit"
            className="new-chat"
            disabled={
              busy ||
              sending ||
              (registering &&
                (!options?.mail_ready || !options.registration_enabled))
            }
          >
            {busy ? "正在提交…" : registering ? "验证并注册" : "登录"}
          </button>
        </form>
        <p className="login-note">
          {registering
            ? "每个邮箱只能注册一个账号，验证码10分钟有效。"
            : "管理员可进入开发模式，使用全部现有功能与管理面板。"}
        </p>
      </div>
    </main>
  );
}
