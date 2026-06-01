#!/bin/sh
# RedisInsight 시작 후 Redis 연결을 자동 등록하는 init 스크립트
# REDIS_PASSWORD 환경변수 필요

# RedisInsight 원래 시작 명령 실행 (WORKDIR: /usr/src/app)
cd /usr/src/app
./docker-entry.sh node redisinsight/api/dist/src/main &
RI_PID=$!

# Node.js로 API 대기 + 설정 초기화 + DB 등록 (BusyBox wget은 PATCH 미지원)
node -e "
const http = require('http');

function request(method, path, body) {
    return new Promise((resolve, reject) => {
        const data = body ? JSON.stringify(body) : null;
        const req = http.request({
            hostname: 'localhost', port: 5540,
            path, method,
            headers: { 'Content-Type': 'application/json' },
            timeout: 5000
        }, res => {
            let buf = '';
            res.on('data', c => buf += c);
            res.on('end', () => resolve({ status: res.statusCode, body: buf }));
        });
        req.on('error', reject);
        req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
        if (data) req.write(data);
        req.end();
    });
}

async function waitReady(maxRetries = 90) {
    for (let i = 0; i < maxRetries; i++) {
        try {
            const r = await request('GET', '/api/databases');
            if (r.status === 200) return r.body;
        } catch {}
        await new Promise(r => setTimeout(r, 1000));
    }
    return null;
}

(async () => {
    console.log('[init] Waiting for RedisInsight...');
    const existing = await waitReady();
    if (existing === null) {
        console.log('[init] Timeout — skipping auto-configuration');
        return;
    }

    // 이미 DB가 등록됐으면 건너뜀 (볼륨 유지 시 재시작 대응)
    if (existing !== '[]') {
        console.log('[init] Redis connection already exists, skipping.');
        return;
    }

    // 1) Settings/EULA 초기화 — encryption: false 필수
    //    Docker 환경에는 system keyring이 없어서 plaintext 저장 사용
    console.log('[init] Initializing settings (EULA + encryption)...');
    const settings = await request('PATCH', '/api/settings', {
        agreements: { eula: true, analytics: false, notifications: false, encryption: false }
    });
    console.log('[init] Settings:', settings.status === 200 ? 'OK' : settings.body);

    // 2) Redis 연결 등록
    console.log('[init] Registering Redis connection (redis:6379)...');
    const db = await request('POST', '/api/databases', {
        name: 'RedGW Redis', host: 'redis', port: 6379,
        password: process.env.REDIS_PASSWORD || ''
    });
    if (db.status === 201 || db.status === 200) {
        console.log('[init] Redis connection registered.');
    } else {
        console.log('[init] Registration failed:', db.body);
    }
})().catch(e => console.error('[init] Error:', e.message));
" &

wait "$RI_PID"
