# AWS Setup — private test server (Mumbai)

**Purpose:** one small server with a **fixed public IP** (Elastic IP) to register with Sharekhan as your static IP, running the app for the read-only Sharekhan test. PAPER/RESEARCH only; live trading does not exist in the code.

**Design:** nothing is opened to the internet except SSH, and only from your own IP. The dashboard and API listen on the server's `127.0.0.1`. You reach them from your Mac through an encrypted SSH tunnel, so your browser still uses `http://127.0.0.1:3300` and the Sharekhan redirect URL stays `http://127.0.0.1:8000/v1/broker/callback`. Calls to Sharekhan's API leave from the Elastic IP.

**Cost (approximate, check AWS pricing):** t3.small around US$16/month, 20 GB disk around US$2, public IPv4 address around US$4. Total **about US$20/month** while it exists. Stopping the instance removes the compute part; the disk and the IP keep billing.

> Never paste the AWS sign-in, the `.pem` key file, or the Sharekhan keys into a chat, email or ticket.

---

## Step 0 — Before you start

1. **Push the latest code to GitHub.** The server downloads the code from GitHub, and the Sharekhan link exists only in the two commits not yet pushed. On your Mac:
   ```bash
   cd ~/Projects/rhythm-ai-trader
   printf "protocol=https\nhost=github.com\n\n" | git credential-osxkeychain erase
   git push
   ```
   Sign in as **babyjose-droid** when asked. Check that github.com/babyjose-droid/RhythmAITradeBot shows the commit "Read-only Sharekhan link…".
2. Sign in to the AWS console (https://console.aws.amazon.com) with your account. Use an IAM user with MFA, not the root user, if you have one.
3. Top-right region menu: choose **Asia Pacific (Mumbai) ap-south-1**. Every step below happens in this region.

## Step 1 — Budget alert (2 minutes, recommended)

1. Search bar: **Budgets** → **Create budget** → **Use a template** → **Monthly cost budget**.
2. Budget amount: **25** (USD). Email: yours. **Create budget**.

You get an email if the month's spending heads past US$25.

## Step 2 — Launch the server

EC2 → **Instances** → **Launch instances**:

| Field | Value |
|---|---|
| Name | `rhythm-trader` |
| Application and OS image | **Ubuntu** → **Ubuntu Server 24.04 LTS**, architecture **64-bit (x86)** |
| Instance type | **t3.small** (2 vCPU, 2 GiB) |
| Key pair | **Create new key pair** → name `rhythm-key`, type **RSA**, format **.pem** → **Create key pair**. The file `rhythm-key.pem` downloads; keep it safe, AWS cannot give it to you again. |
| Network settings | Click **Edit**. Auto-assign public IP: **Enable**. Firewall: **Create security group**, name `rhythm-ssh-only`. Keep only the **ssh** rule, with Source type **My IP**. Do **not** add HTTP or HTTPS rules. |
| Configure storage | **20** GiB, **gp3**. Click **Advanced** → *Encrypted*: **Encrypted** (default KMS key). |
| Advanced details | Leave as is. (*Metadata version* should show **V2 only**.) |

Click **Launch instance**, then **View all instances**. Wait until *Instance state* is **Running** and *Status check* shows **2/2 checks passed**.

## Step 3 — Fixed IP (Elastic IP)

1. EC2 → **Elastic IPs** → **Allocate Elastic IP address** → **Allocate**.
2. Select the new address → **Actions** → **Associate Elastic IP address** → Resource type **Instance** → choose `rhythm-trader` → **Associate**.
3. **Write down this IP.** It is the static IP for the Sharekhan form, and the address you SSH to. Below it is written `ELASTIC_IP`.

## Step 4 — Connect from your Mac

In Terminal on your Mac:

```bash
mkdir -p ~/.ssh
mv ~/Downloads/rhythm-key.pem ~/.ssh/
chmod 400 ~/.ssh/rhythm-key.pem
ssh -i ~/.ssh/rhythm-key.pem ubuntu@ELASTIC_IP
```

Answer **yes** to the fingerprint question the first time. Your prompt changes to `ubuntu@ip-…`. You are now on the server.

## Step 5 — Install the app (on the server)

```bash
git clone https://github.com/babyjose-droid/RhythmAITradeBot.git rhythm-ai-trader
cd rhythm-ai-trader
bash scripts/aws/setup.sh
```

The script adds swap memory, installs Docker, creates `.env` with random secrets (only on the server, permissions 600), and builds and starts the stack. The first run takes **10–20 minutes**. At the end it prints:

- `/health/ready` output (database, scheduler);
- **this server's outgoing IP**. It must equal your Elastic IP. If it shows a different address, check Step 3.

Re-running `bash scripts/aws/setup.sh` later updates to the latest GitHub code and keeps your `.env`.

## Step 6 — Create your sign-in (on the server, once)

```bash
sudo docker compose exec api rhythm-cli user-create --username owner --role owner
```

Choose a password. Add the authenticator secret it prints (shown once) to Google Authenticator, Authy or 1Password.

## Step 7 — Open the dashboard through the tunnel (on your Mac)

1. If the local Docker stack is running on your Mac, stop it first, because ports 3300 and 8000 must be free:
   ```bash
   cd ~/Projects/rhythm-ai-trader && docker compose down
   ```
2. Open a **new** Terminal window and run (leave it open while you use the dashboard):
   ```bash
   ssh -i ~/.ssh/rhythm-key.pem -N -L 3300:127.0.0.1:3300 -L 8000:127.0.0.1:8000 ubuntu@ELASTIC_IP
   ```
   It shows nothing; that is normal. Ctrl+C closes the tunnel.
3. Browse **http://127.0.0.1:3300/login** and sign in with the password and the 6-digit code.

## Step 8 — Create the Sharekhan API key

At sharekhan.com/trading-api → **GET STARTED**:

| Form field | Enter |
|---|---|
| Static IP / whitelisted IP | your **ELASTIC_IP** (IPv4) |
| Redirect URL (if asked) | `http://127.0.0.1:8000/v1/broker/callback` |

If the form rejects the `127.0.0.1` redirect URL, stop and tell me the exact message; do not use a different address without checking.

## Step 9 — Add the Sharekhan keys (on the server)

SSH in (Step 4), then:

```bash
cd ~/rhythm-ai-trader
bash scripts/aws/sharekhan_keys.sh
```

Paste the **API Key**, then the **Secure Key** (32 characters), at hidden prompts. Press Enter at the vendor-key prompt unless Sharekhan gave you one. The script saves them to the server's `.env` and restarts the app.

## Step 10 — The read-only test (dashboard, tunnel open)

**Sharekhan (read only)** page:

1. **Connect Sharekhan** → sign in on Sharekhan's page → you return to the dashboard with "Connected".
2. **Run read-only verification**. Every row should show OK or EMPTY.
3. **Instruments** → **sync** (loads Sharekhan's instrument list).
4. Between **09:15 and 15:30 IST** on a trading day: **Test price stream (10 s)**.
5. If anything shows ERROR, send me the verification table. It holds field names and timings only, no account values.

---

## Day to day

| Task | How |
|---|---|
| Use the dashboard | Step 7 tunnel, then http://127.0.0.1:3300 |
| Sharekhan session | Lasts at most 24 h; click **Connect Sharekhan** again each day |
| Update to the latest code | SSH in → `cd ~/rhythm-ai-trader && bash scripts/aws/setup.sh` |
| Logs | `sudo docker compose logs --tail 100 api` |
| Your home IP changed (SSH times out) | EC2 → Security groups → `rhythm-ssh-only` → Edit inbound rules → Source **My IP** → Save |
| Pause costs | EC2 → instance → **Stop** (disk and IP still bill a little). **Start** again later; the Elastic IP stays the same. |
| Remove everything | Terminate the instance **and** release the Elastic IP. Releasing loses the IP registered with Sharekhan. |

## Security notes

- Only SSH (port 22) is open, and only from your IP; all app ports are bound to `127.0.0.1` on the server.
- Secrets exist only in `~/rhythm-ai-trader/.env` on the server (permissions 600). `RHYTHM_AUTH__SECRET_KEY` there also encrypts the stored Sharekhan session and authenticator secrets: keep it unchanged, or every user must enrol again.
- Ubuntu security updates install automatically (`unattended-upgrades`). Reboot occasionally: `sudo reboot`; the app restarts on its own.
- Backups are written to `~/rhythm-ai-trader/backups` on the same disk. They are not off-host yet (readiness gate OPS-02). An encrypted copy to S3 is the next step when this becomes more than a test.
- This is a private test server, not the hosted production design in DEPLOYMENT_GUIDE.md (TLS, managed database, secrets manager). It runs with `RHYTHM_ENVIRONMENT=local` because you browse it over plain `http://127.0.0.1` through the tunnel.
