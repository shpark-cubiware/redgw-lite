#!/bin/bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# redgw_core Rust 모듈 빌드 스크립트
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# 이 스크립트는 redgw_core PyO3 모듈을 빌드하여 .whl 파일을 생성한다.
# Python 개발자가 Rust 코드를 수정하지 않아도 빌드할 수 있도록 설계되었다.
#
# 사전 준비 (1회만):
#   1) Rust 설치: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
#   2) maturin 설치: pip install maturin
#
# 사용법:
#   ./scripts/rust_build.sh              # 기본: release 빌드
#   ./scripts/rust_build.sh --dev        # 개발용: debug 빌드 (빠르지만 최적화 없음)
#   ./scripts/rust_build.sh --docker     # Docker 이미지에 포함하여 빌드
#
# 출력물:
#   redgw_core/target/wheels/redgw_core-*.whl   (pip install 가능한 패키지)
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUST_MODULE_DIR="$PROJECT_ROOT/redgw_core"

# ── 색상 출력 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── 사전 조건 확인 ──
check_prerequisites() {
    # Rust 툴체인 확인
    if ! command -v rustc &>/dev/null; then
        error "Rust가 설치되어 있지 않습니다."
        echo ""
        echo "  설치 방법: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
        echo "  설치 후:   source \$HOME/.cargo/env"
        echo ""
        exit 1
    fi

    # maturin 확인
    if ! command -v maturin &>/dev/null; then
        error "maturin이 설치되어 있지 않습니다."
        echo ""
        echo "  설치 방법: pip install maturin"
        echo ""
        exit 1
    fi

    # redgw_core 디렉토리 존재 확인
    if [ ! -d "$RUST_MODULE_DIR" ]; then
        error "redgw_core 디렉토리가 없습니다: $RUST_MODULE_DIR"
        echo ""
        echo "  아직 Rust 모듈이 생성되지 않았습니다."
        echo "  빌드 방법은 docs/운영/빌드배포방법.md 를 참고하세요."
        echo ""
        exit 1
    fi

    info "Rust $(rustc --version | awk '{print $2}') / maturin $(maturin --version | awk '{print $2}')"
}

# ── Release 빌드 ──
build_release() {
    info "Release 빌드 시작 (최적화 포함, 시간이 걸릴 수 있음)..."
    cd "$RUST_MODULE_DIR"
    maturin build --release
    info "빌드 완료! wheel 파일:"
    ls -lh target/wheels/*.whl 2>/dev/null || warn "wheel 파일을 찾을 수 없습니다."
}

# ── Dev 빌드 (디버그, 빠름) ──
build_dev() {
    info "개발용 빌드 시작 (최적화 없음, 빠른 컴파일)..."
    cd "$RUST_MODULE_DIR"
    maturin develop
    info "개발 빌드 완료! 현재 Python 환경에 직접 설치됨."
}

# ── Docker 멀티스테이지 빌드 ──
build_docker() {
    info "Docker 이미지 빌드 (Rust 컴파일 + Python 이미지 통합)..."
    cd "$PROJECT_ROOT"

    # Dockerfile에 Rust multi-stage 빌드가 통합되어 있음
    bash build.sh

    local v
    v=$(grep -E '^version' "$PROJECT_ROOT/resource/pyproject.toml" | sed -E 's/.*"(.+)".*/\1/')
    info "Docker 이미지 빌드 완료: redgw:${v}"
}

# ── 빌드 후 설치 안내 ──
show_install_guide() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo " 빌드된 wheel 설치 방법"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  # Docker 컨테이너에 설치 (권장)"
    echo "  docker cp redgw_core/target/wheels/redgw_core-*.whl redgw-app:/tmp/"
    echo "  docker exec redgw-app pip install /tmp/redgw_core-*.whl"
    echo ""
    echo "  # 또는 requirements.txt에 추가"
    echo "  echo 'redgw_core @ file:///app/wheels/redgw_core-0.1.0-*.whl' >> resource/requirements.txt"
    echo ""
    echo "  # 설치 확인"
    echo "  python -c 'import redgw_core; print(dir(redgw_core))'"
    echo ""
    echo "  # Rust 모듈 없이도 RedGW는 정상 동작합니다 (Python fallback)."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ── 메인 ──
main() {
    check_prerequisites

    case "${1:-}" in
        --dev)
            build_dev
            ;;
        --docker)
            build_docker
            ;;
        *)
            build_release
            show_install_guide
            ;;
    esac
}

main "$@"
