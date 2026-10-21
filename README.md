# 로컬 실행

처음 한 번 `bash sh/setup_venv.sh`를 실행하면 프로젝트 전용 Python 가상환경에 필요한 패키지가 설치됩니다. 이후 `bash sh/run_local.sh`로 API 서버와 브라우저 화면을 함께 실행합니다. `http://127.0.0.1:8600`에서 확인할 수 있으며, 종료할 때는 실행 터미널에서 Ctrl+C를 누릅니다. 이 명령은 API 키가 필요 없는 `fake` 모델을 사용하고 LangSmith 추적을 끕니다.

실제 모델을 사용하려면 [상세 실행 안내](doc/README.md)의 Ollama 또는 OpenAI 절차를 따릅니다.

브라우저(Vue)
  → 프런트 서버(:8600)
  → API 서버(:8100)
  → LangGraph 그래프(기본 설정 v2)
  → MCP 클라이언트
  → MCP 서버: ops(모니터링 수치·runbook), docs(서버 탐색용)

그래프 ↔ 선택된 LLM 백엔드(fake / Ollama / OpenAI)
API 서버 ↔ SQLite 세션 저장소
답변·진행 이벤트: API 서버 → 프런트 서버 → 브라우저


