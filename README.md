# MobileWorld Voice Assistant

[MobileWorld](https://github.com/Tongyi-MAI/MobileWorld) 위에서 동작하는 **앱 내장 음성 비서**입니다.
지정한 앱 하나 안에서만 동작합니다. 앱 기능(툴)을 우선 사용하고, 툴이 없는 작업은 화면을 보고 GUI를 조작합니다.

이 저장소에는 직접 작성한 코드만 들어 있습니다. 실행하려면 MobileWorld에 설치해야 합니다.

## 설치

```bash
git clone https://github.com/Tongyi-MAI/MobileWorld.git
git clone https://github.com/skbaek1223/mobileworld-voice-assistant.git

cd MobileWorld
cp -r ../mobileworld-voice-assistant/{src,tests,docs} .
git apply ../mobileworld-voice-assistant/patches/mobileworld.patch
uv sync
uv run pytest tests/assistant -q
```

`patches/mobileworld.patch`는 MobileWorld 원본 파일을 다음과 같이 조금 고칩니다. 기준 커밋은 `a026188`입니다.
- `general_e2e_agent.py`: 하위 클래스가 시스템 프롬프트를 바꿀 수 있도록 `_render_system_prompt()` 메서드를 분리합니다.
- `core/cli.py`, `core/subcommands/__init__.py`: `mw assistant` 명령을 등록합니다.
- `pyproject.toml`: 음성 입출력용 선택 의존성 `voice`(sounddevice, faster-whisper)를 추가합니다.

## 실행

```bash
uv run mw assistant --package com.example.myapp --app-name 마이앱 \
  --model-name <model> --llm-base-url <openai-compatible-url> --api-key $API_KEY \
  --tools-file docs/assistant_tools.example.json \
  --once "최근 주문 내역 알려줘"
```

**폰 없이 늘밥 모의 앱으로 실행하기:** 웹 앱 데이터 폴더를 지정하면 툴만으로 대화합니다.
```bash
NEULBAB_APP_DIR=/path/to/agent-company/product/app uv run mw assistant --mock-neulbab --role parent \
  --model-name <model> --llm-base-url <openai-compatible-url> --api-key $API_KEY
```

구조, 툴 정의 방법, 앱 쪽 Kotlin 예시, 폰에서 직접 실행하도록 옮기는 계획은 [docs/voice-assistant.md](docs/voice-assistant.md)에 있습니다.

## 파일

| 경로 | 내용 |
|---|---|
| `src/mobile_world/assistant/` | 음성 입출력, 기기 제어, 앱 툴, 앱 범위 가드, 에이전트, 세션 |
| `src/mobile_world/assistant/neulbab/` | 늘밥 툴 계약을 구현한 모의 앱 (웹 `mock_api.js` 로직 이식) |
| `src/mobile_world/core/subcommands/assistant.py` | `mw assistant` CLI |
| `tests/assistant/` | 기기, LLM, 오디오 없이 돌아가는 테스트 |
| `docs/` | 문서, 툴 정의 예시 |
| `docs/neulbab/` | 늘밥 앱용 툴 계약 v0.1 ([설명](docs/neulbab/tool_contract.md), [정의](docs/neulbab/tools.json)) |
| `patches/mobileworld.patch` | MobileWorld 원본 수정분 |
