# คำสั่ง Git สำหรับ Physics Plus

ใช้ลำดับเดียวกับงานเว็บ CV: กิ่งงาน → merge เข้า `DevTutor` เพื่อทดสอบ Preview → merge เข้า `main` หลังตรวจผ่าน โดยใช้ `--no-ff` เพื่อให้ประวัติของแต่ละช่วงเห็นชัด คำสั่งด้านล่างเป็นแม่แบบสำหรับงานรอบถัดไป ให้เปลี่ยนชื่อกิ่งและข้อความ commit ตามงานจริง

ก่อน push ไป `main` ให้ทำตาม [คู่มือติดตั้ง](DEPLOY.md): สำรองฐานข้อมูล, รัน `python3 manage.py init` กับฐาน Preview แยก, ทดสอบคอร์ส/ห้อง/Queue/Google Sheet บน Preview, แล้วเตรียม migration ฐาน Production เพราะการ push `main` อาจทำให้ Vercel เผยแพร่โค้ดอัตโนมัติ

```bash
cd /Users/engineer/Documents/Codex/2026-10-18/z/work/physics-house-git
git status --short
git add -A
git diff --cached --check
git diff --cached --stat
node --check public/live.js
python3 -m unittest discover -q
git commit -m "Complete courses, Sheet delivery, and login performance"
git fetch origin
git push -u origin <feature-branch>

git switch DevTutor
git pull --ff-only origin DevTutor
git merge --no-ff <feature-branch> -m "Merge tested feature into DevTutor"
git push origin DevTutor
```

ตรวจ Preview ของ `DevTutor` ด้วยฐาน Preview ที่แยกจาก Production ตาม [คู่มือติดตั้ง](DEPLOY.md) ทดสอบเข้าสู่ระบบทั้งสามบทบาท, คอร์ส/ห้องเรียน, ชำระเงิน, ค่าจ้าง และชีต พร้อมวัดความเร็วหลังปล่อยจริง เมื่อผลผ่านจึงใช้คำสั่งต่อไปนี้:

```bash
cd /Users/engineer/Documents/Codex/2026-10-18/z/work/physics-house-git
git switch main
git pull --ff-only origin main
git merge --no-ff DevTutor -m "Merge tested Physics Plus release into main"
git push origin main

git log --graph --oneline --decorate --all -20
```

หาก `git diff --cached --stat` มีไฟล์ที่ไม่ตั้งใจ อย่า commit จนกว่าจะเอาออกจาก stage ถ้ามีข้อความ merge conflict ให้แก้ในไฟล์ ทดสอบซ้ำ แล้วจึง `git add` และ `git commit` เพื่อจบ merge อย่าใช้ `git push --force`
