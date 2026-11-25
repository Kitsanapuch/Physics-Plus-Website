# คำสั่ง Git สำหรับ Physics Plus

ใช้ลำดับเดียวกับงานเว็บ CV: กิ่งงาน → merge เข้า `DevTutor` เพื่อทดสอบ Preview → merge เข้า `main` หลังตรวจผ่าน โดยใช้ `--no-ff` เพื่อให้ประวัติของแต่ละช่วงเห็นชัด ไม่ใช้ `git graft`, rebase ประวัติที่เผยแพร่แล้ว หรือ force push คำสั่งด้านล่างเป็นแม่แบบสำหรับงานรอบถัดไป ให้เปลี่ยนชื่อกิ่ง ไฟล์ และข้อความ commit ตามงานจริง

ก่อน push ไป `main` ให้ทำตาม [คู่มือติดตั้ง](DEPLOY.md): สำรองฐานข้อมูล, รัน `python3 manage.py init` กับฐาน Preview แยก, ทดสอบคอร์ส/ห้อง/Queue/Google Sheet บน Preview, แล้วเตรียม migration ฐาน Production เพราะการ push `main` อาจทำให้ Vercel เผยแพร่โค้ดอัตโนมัติ

```bash
cd /Users/engineer/Documents/Codex/2026-10-18/z/work/physics-house-git
git fetch origin main DevTutor
git switch DevTutor
git merge --ff-only origin/DevTutor
git switch -c feature/<topic>

# แก้ไฟล์และทดสอบก่อน จากนั้น stage เฉพาะไฟล์ที่ตั้งใจเผยแพร่
git status --short
git add public/index.html public/portal.css
git diff --cached --check
git diff --cached --name-only
git diff --cached
node --check public/live.js
python3 -m unittest discover -q
git commit -m "Describe the completed change"
git push -u origin feature/<topic>

git fetch origin main DevTutor
git switch DevTutor
git merge --ff-only origin/DevTutor
git merge --no-ff feature/<topic> -m "Merge tested feature into DevTutor"
git push origin DevTutor
```

ตรวจ Preview ของ `DevTutor` ด้วยฐาน Preview ที่แยกจาก Production ตาม [คู่มือติดตั้ง](DEPLOY.md) ทดสอบเข้าสู่ระบบทั้งสามบทบาท, คอร์ส/ห้องเรียน, ชำระเงิน, ค่าจ้าง และชีต พร้อมวัดความเร็วหลังปล่อยจริง เมื่อผลผ่านจึงใช้คำสั่งต่อไปนี้:

```bash
cd /Users/engineer/Documents/Codex/2026-10-18/z/work/physics-house-git
git fetch origin main DevTutor
git switch main
git merge --ff-only origin/main
git merge --no-ff origin/DevTutor -m "Release tested Physics Plus changes"
git diff --check HEAD^1 HEAD
git push origin main

git log --graph --oneline --decorate --all -20
```

ก่อน commit ตรวจว่าไม่มี `.env`, ฐานข้อมูล, log, กุญแจ, token หรือข้อมูลจริงของนักเรียน/ครูใน staged diff หากมีไฟล์ที่ไม่ตั้งใจให้ใช้ `git restore --staged <path>` แล้วตรวจซ้ำ ถ้ามี merge conflict ให้แก้ในไฟล์ ทดสอบซ้ำ แล้วจึง `git add` และ `git commit` เพื่อจบ merge อย่าใช้ `git push --force`
