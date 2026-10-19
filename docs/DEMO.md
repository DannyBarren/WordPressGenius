# WordPressGenius demo walkthrough

Text-only path through a first local run. No screenshots. You need Python 3.10+ and a browser. A WordPress Application Password is optional for the draft/publish/refund beats; without it you can still log in and see the chat UI.

## 1. Install and open the UI

```bash
git clone https://github.com/DannyBarren/WordPressGenius.git
cd WordPressGenius
cp .env.example .env
cp config/users.example.yml config/users.yml
python launch.py
```

What you should see:

1. The terminal prints that Streamlit is opening `http://localhost:8501`.
2. On a cold machine, the browser shows the setup screen. Click the install button. It installs `requirements.txt`, imports `core`, `agents`, and `tools`, and runs two readiness tests.
3. After setup, the login form appears.

If you already installed dependencies, `streamlit run app.py` is equivalent.

## 2. Sign in

Use an example account from `config/users.example.yml`:

- username: `admin`
- password: `change-me`

That password is a documented fake. The stored value is a legacy SHA-256 hex digest. The first successful login rewrites that user to a salted Argon2id `password_hash`. Open `config/users.yml` after login if you want to see the upgrade. Then change the password before anyone else can reach the app:

```bash
python -c "from core.security import hash_password; print(hash_password('your-new-password'))"
```

Replace the `admin` hash with the quoted Argon2id string.

## 3. Persist the credential vault (recommended)

In a second terminal:

```bash
python -c "from core.security import generate_fernet_key; print(generate_fernet_key())"
```

Put the output in `.env` as `CREDENTIAL_ENCRYPTION_KEY`. Without it, saved sites are encrypted with an ephemeral key and will not survive a restart.

## 4. Connect a WordPress site

In WordPress: Users → Profile → Application Passwords → create one named `wordpressgenius`.

In the app sidebar:

1. Site URL, e.g. `https://your-store.example`
2. WordPress username
3. Application Password
4. Click **Test WordPress connection**

A healthy result shows the site name and the connected user's roles. Saved sites appear in the site picker.

No live site? Skip this step. You can still type prompts; the Executor will say credentials are missing and will not write anything.

## 5. Draft (no extra approval)

In chat:

```text
Create a draft blog post about summer hours.
```

Expected:

- Planner proposes `create_post` with `status: draft`.
- SafetyLayer does not require confirmation.
- If WordPress is connected, a draft appears in Posts. If not, you get a credentials message instead of a publish.

## 6. Publish (must wait)

```text
Publish post #42 now.
```

Expected, even if you also say "don't ask":

- The plan includes an update or create with `status: publish`.
- The Executor stops. The UI shows an approval card and a before/after preview when the post can be read.
- No `POST`/`PUT` to the write endpoint until you approve.
- After approval, a JSON snapshot is written under `data/backups/` (when the resource is readable), then the write runs.

## 7. Delete (must wait)

```text
Delete page #4.
```

Expected: confirmation, then a snapshot of page 4 if the REST read succeeds, then delete. The approval card should treat this as high risk.

## 8. Refund (must wait, and it is not undoable)

```text
Refund order #100.
```

Expected:

- Operation is `stripe_refund`.
- Approval is required.
- The preview should say the refund is irreversible once Stripe accepts it.
- Undo will not reverse a completed Stripe refund.

## 9. Read-only contrast

```text
Show me the recent orders.
```

Expected: `woocommerce_read`, no approval card, no backup.

## 10. Optional: no API key vs one API key

With no `OPENAI_API_KEY` / Anthropic / Groq / Gemini key, the rule-based planner and template copy run. With one key in `.env` or the settings page, every agent uses that provider. The approval gates do not change.

## 11. What not to expect

- A hosted multi-user SaaS login (this is YAML users on one process).
- Full-site restore from `data/backups/` (those are REST resource snapshots).
- The model to be allowed to skip approval because it labeled the action `risk: low`.
