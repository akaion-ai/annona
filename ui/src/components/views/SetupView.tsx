/**
 * First-run configurator: choose the policy before using the machine.
 *
 * Until this screen existed, an install from the .dmg reached the Ask box with
 * whatever policy happened to be on disk — usually none, in which case the
 * kernel was not enforcing and the status line said so in small type. The one
 * decision the product is *about* was the one decision it never asked about.
 *
 * Three questions, the same three the CLI asks, from the same profiles
 * (runner/policy/profiles.py), so the terminal and the window cannot drift into
 * offering different things.
 *
 * Each profile is shown by its consequence rather than its settings: "adds a
 * substrate capped at public" describes YAML, and "the hosted provider can only
 * ever see material classified public" is what somebody is agreeing to. The
 * full sentence is on the card. Nothing here is behind a tooltip.
 *
 * This screen appears once, on a machine with no policy. It cannot change an
 * existing one — the daemon refuses that — and it does not pretend to offer it.
 */
import { useEffect, useState } from "react"
import { kernel, SetupOptions, PolicyProfile, FrontierProviderInfo } from "../../api/kernel"

interface Props {
  /** Called once a policy exists on disk. */
  onDone: () => void
}

const DEFAULT_FOLDERS = "~/Documents, ~/Downloads"

/** A person types a folder; the policy matches a glob. Without this, typing
 *  exactly what is suggested grants the directory entry and nothing in it. */
function asGlobs(input: string): string[] {
  return input
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((p) => (p.endsWith("**") ? p : `${p.replace(/\/$/, "")}/**`))
}

export default function SetupView({ onDone }: Props) {
  const [options, setOptions] = useState<SetupOptions | null>(null)
  const [error, setError]     = useState<string | null>(null)
  const [saving, setSaving]   = useState(false)

  const [model, setModel]       = useState("")
  const [profileId, setProfile] = useState("local-only")
  const [folders, setFolders]   = useState(DEFAULT_FOLDERS)
  const [providerId, setProviderId] = useState("")
  const [providerModel, setProviderModel] = useState("")
  const [providerEndpoint, setProviderEndpoint] = useState("")
  const [keyEnv, setKeyEnv]     = useState("")

  useEffect(() => {
    let cancelled = false
    kernel
      .setupOptions()
      .then((o) => {
        if (cancelled) return
        setOptions(o)
        setModel(o.suggested_model)
        const recommended = o.profiles.find((p) => p.recommended)
        if (recommended) setProfile(recommended.id)
        // If the machine is already governed there is nothing to ask, and this
        // screen must not stand between the person and the app.
        if (o.configured) onDone()
      })
      .catch((e) => !cancelled && setError(e?.message ?? "Could not reach the daemon"))
    return () => { cancelled = true }
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  const profile: PolicyProfile | undefined = options?.profiles.find((p) => p.id === profileId)
  const provider: FrontierProviderInfo | undefined = options?.providers.find(
    (p) => p.id === providerId,
  )

  const pickProvider = (id: string) => {
    setProviderId(id)
    const match = options?.providers.find((p) => p.id === id)
    setProviderModel(match?.model ?? "")
    setProviderEndpoint(match?.endpoint ?? "")
    setKeyEnv(match?.api_key_env ?? "")
  }

  const write = async () => {
    if (!profile) return
    setSaving(true)
    setError(null)
    try {
      await kernel.createPolicy({
        profile: profile.id,
        model,
        readable_paths: profile.id === "read-nothing" ? [] : asGlobs(folders),
        ...(profile.needs_frontier
          ? {
              provider: providerId,
              provider_model: providerModel,
              provider_endpoint: providerEndpoint,
              api_key_env: keyEnv,
            }
          : {}),
      })
      onDone()
    } catch (e: any) {
      setError(e?.message ?? "Could not write the policy")
    } finally {
      setSaving(false)
    }
  }

  if (error && !options) {
    return (
      <div className="an-setup">
        <div className="an-setup__card">
          <div className="an-setup__error">{error}</div>
        </div>
      </div>
    )
  }

  if (!options) {
    return (
      <div className="an-setup">
        <div className="an-setup__card"><div className="text-muted">Asking the daemon…</div></div>
      </div>
    )
  }

  const needsProvider = Boolean(profile?.needs_frontier)
  const canWrite = !saving && Boolean(profile) && (!needsProvider || Boolean(providerId))

  return (
    <div className="an-setup">
      <div className="an-setup__card">
        <header className="an-setup__head">
          <h1>Set up Annona</h1>
          <p>
            Three questions. They become <code>{options.policy_path}</code>, which is the
            document every decision this machine takes is derived from — and the one you
            hand an auditor.
          </p>
        </header>

        {/* 1 ── the local model */}
        <section className="an-setup__step">
          <h2>1. Which local model?</h2>
          {options.runtime.reachable ? (
            options.runtime.models.length > 0 ? (
              <div className="an-setup__models">
                {options.runtime.models.map((name) => (
                  <button
                    key={name}
                    className={`an-setup__chip ${model === name ? "on" : ""}`}
                    onClick={() => setModel(name)}
                  >
                    {name}
                    {name === options.suggested_model && <span className="an-setup__hint"> · suggested</span>}
                  </button>
                ))}
              </div>
            ) : (
              <div className="an-setup__warn">
                The runtime at {options.runtime.endpoint} is up but has no models.
                Pull one: <code>ollama pull {options.suggested_model}</code>
              </div>
            )
          ) : (
            <div className="an-setup__warn">
              No model runtime is answering at {options.runtime.endpoint}. Install Ollama
              from ollama.com and start it — the policy will name{" "}
              <code>{options.suggested_model}</code> until then.
            </div>
          )}
        </section>

        {/* 2 ── what may leave */}
        <section className="an-setup__step">
          <h2>2. What may leave this machine?</h2>
          <div className="an-setup__profiles">
            {options.profiles.map((p) => (
              <button
                key={p.id}
                className={`an-setup__profile ${profileId === p.id ? "on" : ""}`}
                onClick={() => setProfile(p.id)}
              >
                <div className="an-setup__profile-title">
                  {p.title}
                  {p.recommended && <span className="an-setup__badge">recommended</span>}
                </div>
                <div className="an-setup__profile-consequence">{p.consequence}</div>
              </button>
            ))}
          </div>
        </section>

        {/* 2b ── the provider, only when the profile registers one */}
        {needsProvider && (
          <section className="an-setup__step">
            <h2>Which hosted provider?</h2>
            <div className="an-setup__models">
              {options.providers.map((p) => (
                <button
                  key={p.id}
                  className={`an-setup__chip ${providerId === p.id ? "on" : ""}`}
                  onClick={() => pickProvider(p.id)}
                >
                  {p.title}
                  <span className="an-setup__hint"> · {p.jurisdiction}</span>
                </button>
              ))}
            </div>

            {provider && (
              <div className="an-setup__fields">
                <label>
                  Model
                  <input value={providerModel} onChange={(e) => setProviderModel(e.target.value)} />
                </label>
                {provider.id !== "anthropic" && (
                  <label>
                    Endpoint
                    <input
                      value={providerEndpoint}
                      onChange={(e) => setProviderEndpoint(e.target.value)}
                      placeholder="https://…/v1"
                    />
                  </label>
                )}
                <label>
                  Environment variable holding the key
                  <input value={keyEnv} onChange={(e) => setKeyEnv(e.target.value)} />
                </label>
                {/* Said here rather than discovered in a git history. */}
                <div className="an-setup__note">
                  The policy stores the <em>name</em>. The key itself is never written to
                  this file — export it in the environment the daemon runs in.
                </div>
              </div>
            )}
          </section>
        )}

        {/* 3 ── what may be read */}
        {profileId !== "read-nothing" && (
          <section className="an-setup__step">
            <h2>3. Which folders may be read?</h2>
            <input
              className="an-setup__input"
              value={folders}
              onChange={(e) => setFolders(e.target.value)}
              placeholder={DEFAULT_FOLDERS}
            />
            <div className="an-setup__note">
              Credentials, keys and <code>~/.ssh</code> are refused to every tool
              regardless of what you put here.
            </div>
          </section>
        )}

        {/* What is about to be written, in words, before it is written. */}
        {profile && (
          <div className="an-setup__summary">
            <strong>{profile.title}.</strong> {profile.consequence}
          </div>
        )}

        {error && <div className="an-setup__error">{error}</div>}

        <button className="ak-btn-primary an-setup__go" onClick={write} disabled={!canWrite}>
          {saving ? "Writing…" : "Write the policy"}
        </button>
      </div>
    </div>
  )
}
