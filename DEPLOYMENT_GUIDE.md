# TradeBot SaaS - Deployment Guide

Aapke Hetzner server par **Coolify** pehle se chal raha hai. Aapke CRM ke containers bhi Coolify hi manage kar raha hai. Isliye, TradeBot SaaS ko deploy karne ke 2 aasan tareeqe hain:

---

## Method 1: Coolify Dashboard Se Deploy Karna (Recommended)

Kyunki Coolify automatic SSL certificate generation (Let's Encrypt), auto-builds, aur reverse proxy khud handle karta hai, ye tareeqa sabse aasan aur secure hai.

### Step 1: Code Ko GitHub Par Push Karein
Coolify repositories se build karta hai. Is project ko apne GitHub account par push karein (Private ya Public dono chalenge):
1. GitHub par ek naya repository banayein.
2. Apne local system par terminal khol kar ye commands run karein:
   ```bash
   git init
   git add .
   git commit -m "initial commit"
   git branch -M main
   git remote add origin YOUR_GITHUB_REPO_URL
   git push -u origin main
   ```

### Step 2: Coolify Dashboard Mein Project Add Karein
1. Apne browser mein Coolify dashboard kholin (usually `http://204.168.233.249:8000`).
2. **Projects** par jayein -> Apne project ko select karein -> **New Resource** par click karein.
3. **Public Repository** ya **Private Repository** (agar private repo hai) select karein aur apne GitHub repo ka link enter karein.
4. Coolify repo ko scan karega. **Build Pack** mein **Dockerfile** select karein.

### Step 3: Domain Aur Ports Configure Karein
1. Application settings mein **Domains** field mein apna domain ya subdomain enter karein (jaise `tradebot.yourdomain.com`).
2. **Port** field mein `5000` enter karein. (Internal container port 5000 hai, Coolify isko automatically aapke domain ke port 80/443 par expose kar dega, jisse port conflict bilkul nahi hoga).

### Step 4: Environment Variables Set Karein
Coolify dashboard ke **Environment Variables** tab mein niche diye gaye variables add karein:
* `FLASK_SECRET` = random_secret_string
* `ADMIN_SECRET` = YourAdminPassword! (Admin panel login password)
* `ENCRYPTION_KEY` = 32_char_random_key_for_api_keys (API keys encrypt karne ke liye)
* `DB_PATH` = `/app/db_volume/database.json`
* `PORT` = `5000`
* `HOST` = `0.0.0.0`

### Step 5: Persistent Storage Mount Karein
Aapka data reset na ho jab container rebuild ho, isliye storage mount karna zaroori hai:
1. Coolify dashboard mein application settings ke **Storages** tab par jayein.
2. Naya volume add karein:
   * **Destination Path (Inside Container):** `/app/db_volume`
   * **Destination Path for logs:** `/app/logs`
   * **Destination Path for bot logs:** `/app/bot-engine/logs`
3. Save par click karein.

### Step 6: Deploy Par Click Karein!
* Settings save karke **Deploy** button daba dein. Coolify repository se code pull karega, Docker image build karega, SSL bind karega aur site live ho jayegi.

---

## Method 2: Direct SSH (Docker Compose) Se Deploy Karna

Agar aap Coolify dashboard use nahi karna chahte aur direct terminal se deploy karna chahte hain:

### Step 1: Code Server Par Upload Karein
1. Local terminal se project folder ko zip karein aur server par upload karein:
   ```bash
   scp -r d:\weex root@204.168.233.249:/root/tradebot-saas
   ```

### Step 2: SSH Se Login Karein
```bash
ssh root@204.168.233.249
cd /root/tradebot-saas
```

### Step 3: Environment Variables Update Karein
`docker-compose.yml` file khol kar variables check karein (aap `nano docker-compose.yml` chala kar edit kar sakte hain):
* `FLASK_SECRET`, `ADMIN_SECRET`, aur `ENCRYPTION_KEY` ko safe values se badlein.

### Step 4: Deploy Karein
Niche di gayi command run karein:
```bash
docker compose up -d --build
```
Ye command Docker image ko build karegi aur background mein live kar degi.

### Step 5: Access Karein
Aapki SaaS app ab ready hai! Aap isko is URL par access kar sakte hain:
`http://204.168.233.249:5005`
