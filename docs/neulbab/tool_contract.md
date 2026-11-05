# 늘밥 음성 비서 툴 계약 v0.3

이 문서는 **늘밥 Android 앱이 음성 비서에게 열어 줄 기능**을 정합니다.
앱과 음성 비서를 따로 만들어도 이 계약만 지키면 서로 맞물립니다.

- 원본 정의: [`tools.json`](tools.json). 이름, 인자(JSON Schema), 반환 필드, 오류 코드가 들어 있습니다. 이 문서와 다르면 `tools.json`이 맞습니다.
- 기준 동작: 웹 프로토타입 `agent-company/product/app/mock_api.js`(v4). 각 툴의 `web_ref`에 대응하는 함수를 적어 두었습니다.
- 음성 비서는 이 파일을 그대로 읽습니다: `mw assistant --tools-file docs/neulbab/tools.json --role parent ...`

## 1. 원칙

1. **역할 분리.** 툴마다 쓸 수 있는 역할(`parent` = 어르신, `guardian` = 보호자)이 정해져 있습니다. 음성 비서는 현재 역할의 툴만 받습니다. 앱도 역할을 다시 확인해서, 맞지 않으면 `WRONG_ROLE`을 돌려줍니다.
2. **주문과 설정 변경은 앱이 확인을 강제합니다.** `prepare_*`가 확인 번호(`confirmation_id`)와 확인 질문을 주고, 사용자가 동의해야만 `place_order` / `apply_settings_change`가 실행됩니다. LLM이 확인 질문을 빼먹어도 확인 번호가 없으면 주문이 나가지 않습니다(헌장 9절 "주문 전 확인 1회").
3. **안전 필터는 우회할 수 없습니다.** 알레르기, 필수 태그, 건강 조건 필터는 앱이 `prepare_order`와 `place_order`에서 **매번 다시** 검사합니다. 사용자가 메뉴 이름을 직접 말해도, 필터를 통과하지 못하면 `MENU_NOT_ALLOWED`입니다(웹의 `passesFilter`, "완화 금지").
4. **말할 문구는 앱이 줍니다.** 성공 응답의 `say`와 오류 응답의 `message`는 웹 앱 화면과 같은 쉬운 말입니다. 조사(을/를, 이/가)와 호칭(어머님, 따님)도 이미 맞춰져 있습니다. 음성 비서는 이 문장을 그대로 읽거나 짧게 다듬어 씁니다.
5. **모의 주문입니다.** 실제 결제·주문·배송은 하지 않습니다(헌장 2절). 플랫폼은 "가/나/다 배달앱" 모의 어댑터입니다.
6. **건강 정보는 의료 조언이 아닙니다.** 건강 조건이 있으면 `disclaimer`("의사·영양사 상담을 대신하지 않아요")를 함께 돌려주고, 음성 비서는 처음 안내할 때 이 문장을 읽어 줍니다.

## 2. 응답 형식

모든 툴이 같은 봉투(envelope)를 씁니다.

```json
{"ok": true,  "data": { ... }, "say": "전복죽을 주문했어요. 오후 12시 5분쯤 도착해요. 자녀분께 알림이 가요"}
{"ok": false, "error": {"code": "CANNOT_CANCEL", "message": "이미 음식을 만들기 시작해서 취소할 수 없어요"}}
```

## 3. 툴 목록

| 툴 | 어르신 | 보호자 | 종류 | 하는 일 | 웹 대응 |
|---|:-:|:-:|---|---|---|
| `get_context` | ✓ | ✓ | 읽기 | 현재 역할, 호칭, 설정 유무, 진행 중 주문, 현재 시각 | `getSession`, `loadSettings` |
| `get_recommendations` | ✓ | ✓ | 읽기 | 추천 메뉴 3개(+`include_more`면 다른 메뉴 6개), 건강 안내 | `recommend`, `moreMenus` |
| `get_menu_detail` | ✓ | ✓ | 읽기 | 메뉴 설명, 주문 가능 여부. 보호자에게는 영양 수치, 출처, 조건별 판정까지 | `nutritionRows`, `healthResultLines` |
| `prepare_order` | ✓ | ✓ | 확인 발급 | 주문 1단계: 검사 후 확인 번호와 확인 질문 발급 | `pa2View`, `ch4View` |
| `place_order` | ✓ | ✓ | **쓰기** | 주문 2단계: 확인 번호로 주문. 주문자는 역할에서 정함 | `createOrder` |
| `get_order_status` | ✓ | ✓ | 읽기 | 최근 주문의 단계, 주문자, 도착 시각, 취소 가능 여부 | `getStatus`, `orderedByText` |
| `cancel_order` | ✓ | | **쓰기** | 접수 단계일 때만 취소 | `cancelOrder` |
| `acknowledge_arrival` | ✓ | | **쓰기** | 도착한 주문에 "잘 받았어요" 표시 | `acknowledgeOrder` |
| `list_orders` | | ✓ | 읽기 | 주문 내역 | CH-H |
| `list_notifications` | | ✓ | 읽기 | 알림함 | CH-N |
| `get_settings` | | ✓ | 읽기 | 현재 설정과 고를 수 있는 값 | `loadSettings` |
| `prepare_settings_change` | | ✓ | 확인 발급 | 설정 변경 1단계: 변경 요약과 변경 후 남는 메뉴 수 | `validateSettings` |
| `apply_settings_change` | | ✓ | **쓰기** | 설정 변경 2단계 | `saveSettings` |
| `open_screen` | ✓ | ✓ | 화면 이동 | 해당 화면을 띄워 사용자가 눈으로도 확인하게 함 | 화면 id |

어르신은 9개, 보호자는 12개 툴을 받습니다.

## 4. 확인 번호 규칙

`prepare_order`와 `prepare_settings_change`가 발급하는 `confirmation_id`의 규칙입니다. 앱이 지켜야 합니다.

- **한 번만 씁니다.** 한 번 쓰면 무효가 됩니다.
- **120초 뒤 만료됩니다.** 만료되거나 모르는 번호면 `CONFIRMATION_INVALID`입니다.
- **발급한 역할에 묶입니다.** 어르신이 받은 번호를 보호자가 쓸 수 없습니다.
- **발급한 내용에 묶입니다.** 번호를 쓸 때 메뉴나 변경 내용을 다시 받지 않습니다. 발급할 때 저장한 내용만 실행합니다.
- (v0.3) 추천 메뉴에 `tags`(부드러운 음식, 저염 등)와 `description`을 함께 돌려줍니다. 비서는 메뉴를 이 사실로만 설명합니다(평가에서 "부드러운 거"에 부드럽지 않은 메뉴를 권하는 오류가 나와서 추가).
- **짝이 되는 실행 툴은 `commit_tool`에 적습니다** (v0.2). `prepare_order` → `place_order`, `prepare_settings_change` → `apply_settings_change`. 음성 비서 세션은 이 짝을 보고, 확인 질문을 LLM 없이 직접 읽고 예/아니요를 판별해 실행합니다. 확인 번호는 LLM에게 보여 주지 않습니다.
- **실행할 때 다시 검사합니다.** 발급 뒤에 설정이 바뀌거나 다른 주문이 들어왔으면 그에 맞는 오류를 돌려줍니다(`MENU_NOT_ALLOWED`, `ACTIVE_ORDER_EXISTS` 등).

## 5. 대표 흐름

**어르신: "점심 뭐 먹을까?" → 주문**
```
get_context            → role=parent, active_order_id=null
get_recommendations    → 들깨미역국 정식 / 가자미조림 정식 / 전복죽, health_note
answer/ask_user        "오늘은 들깨미역국, 가자미조림, 전복죽이 있어요. 어떤 걸로 드릴까요?"
(사용자) "전복죽"
prepare_order m04      → confirmation_id, confirm_question
ask_user               "전복죽, 10,000원이에요. 자녀분 카드로 결제돼요. 주문할까요?"
(사용자) "응"
place_order            → say "전복죽을 주문했어요. 오후 12시 5분쯤 도착해요. 자녀분께 알림이 가요"
open_screen order_status
answer                 (say를 읽음)
```

**어르신: "취소해 줘"** → `get_order_status`(`can_cancel` 확인) → `ask_user` "주문을 취소할까요?" → `cancel_order(order_id)`. 조리가 시작됐으면 `CANNOT_CANCEL` 문구를 읽어 줍니다.

**보호자: "엄마 저녁 대신 주문해 줘"** → `get_recommendations` → `prepare_order`(오류가 `GUARDIAN_BLOCKED`면 그대로 안내) → 확인 → `place_order`. 어르신 화면에는 웹과 같은 대리 주문 알림("따님이 저녁으로 ○○을 주문했어요. ○시쯤 도착해요")이 뜹니다.

**보호자: "엄마 당뇨 있다고 설정해 줘"** → `get_settings` → `prepare_settings_change(health_conditions=[기존 + "diabetes"])` → 확인 질문을 읽을 때 `menus_after`가 0이면 "바꾸면 고를 수 있는 메뉴가 없어요"라고 함께 알림 → `apply_settings_change`.

## 6. 오류 코드

| 코드 | 언제 | 말할 문구 |
|---|---|---|
| `NOT_LOGGED_IN` | 세션 없음 | 먼저 늘밥에 들어와 주세요 |
| `WRONG_ROLE` | 역할에 없는 툴 | 이 일은 지금 화면에서 할 수 없어요 |
| `NO_SETTINGS` | 보호자 설정 전 | 아직 자녀분이 설정을 하지 않았어요. 자녀분께 늘밥 설정을 부탁해 주세요 |
| `INVALID_ARGUMENT` | 스키마에 맞지 않는 인자 | 요청을 이해하지 못했어요. 다시 말씀해 주세요 |
| `MENU_NOT_FOUND` | 없는 메뉴 id | 그런 메뉴는 없어요 |
| `MENU_NOT_ALLOWED` | 알레르기·태그·건강 필터에 걸림, 판매 중지 | 그 메뉴는 지금 고르실 수 없어요. 건강이나 알레르기 조건에 맞지 않아요 |
| `ACTIVE_ORDER_EXISTS` | 어르신 주문 중 진행 중인 주문 있음 | 이미 오고 있는 주문이 있어요. 도착한 뒤에 다시 주문할 수 있어요 |
| `GUARDIAN_BLOCKED` | 대리 주문 불가(`canGuardianOrder` 거짓) | 지금 음식이 오고 있어요. 도착하면 다시 대신 주문할 수 있어요 |
| `NO_ORDER` | 주문 없음 | 아직 주문이 없어요 |
| `ORDER_MISMATCH` | 넘긴 `order_id`가 최근 주문이 아님 | 말씀하신 주문을 찾지 못했어요. 주문 상태를 다시 확인할게요 |
| `CANNOT_CANCEL` | 접수 단계가 지남 | 이미 음식을 만들기 시작해서 취소할 수 없어요 |
| `NOT_ARRIVED` | 도착 전 "잘 받았어요" | 아직 음식이 도착하지 않았어요 |
| `CONFIRMATION_INVALID` | 확인 번호 만료·재사용·불일치 | 확인 시간이 지났어요. 처음부터 다시 해 주세요 |
| `PLATFORM_ERROR` | 모의 어댑터 실패 | 배달앱에 연결하지 못했어요. 잠시 뒤에 다시 해 주세요 |
| `STORAGE_ERROR` | 저장 실패 | 저장하지 못했어요. 한 번 더 말씀해 주세요 |

## 7. 음성으로 열지 않는 것

| 기능 | 이유 |
|---|---|
| 로그인, 회원가입, 가족 코드 발급, 로그아웃 | 계정과 세션은 사람이 직접 다룹니다 |
| 주소·결제 수단 변경 | 음성 오인식 위험이 크고 개인정보입니다. 화면에서만 바꿉니다 |
| 처음 상태로 되돌리기 | 되돌릴 수 없는 일괄 삭제입니다 |
| A/B 디자인 변경 | 실험 조건이라 사용자가 바꾸지 않습니다 |
| 보호자의 주문 취소 | 웹 프로토타입에서도 어르신 화면에만 있습니다. 필요하면 v0.2에서 검토합니다 |

이 기능들은 툴이 없을 뿐 아니라, 음성 비서가 GUI로 대신 누르는 것도 막아야 합니다. 이 부분은 v0.2에서 처리할 예정입니다(10절 참고).

## 8. Android 앱 구현 가이드

**지금(ADB 프로토타입):** 각 툴은 broadcast 하나에 대응합니다.
```
am broadcast -a com.neulbab.app.assistant.PREPARE_ORDER -p com.neulbab.app --es menu_id m04
```
- 인자는 모두 문자열 extra로 옵니다. 숫자, 불리언, 배열은 JSON 문자열입니다(`--es include_more true`, `--es health_conditions '["diabetes"]'`).
- 결과는 `setResultData(<2절의 JSON 봉투>)`로 돌려줍니다. 음성 비서는 이 값을 툴 결과로 받습니다.
- 수신부는 **디버그 빌드에만** 넣습니다. exported receiver는 다른 앱도 호출할 수 있기 때문입니다.

```kotlin
class AssistantReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val tool = intent.action!!.removePrefix("com.neulbab.app.assistant.")
        val args = intent.extras?.keySet()?.associateWith { intent.getStringExtra(it) } ?: emptyMap()
        setResultData(AssistantTools.call(tool, args).toJson())   // 앱의 기존 로직을 그대로 호출
    }
}
```

**나중(앱 내장):** 같은 `AssistantTools.call(name, args)`를 앱 안의 음성 비서가 직접 호출합니다. broadcast와 ADB는 빠지고, 툴 이름, 인자, 응답 형식은 바뀌지 않습니다.

**앱 로직 구조 권장:** 웹의 `mock_api.js`처럼, 추천·상태·문구 생성은 화면과 분리된 순수 함수(Kotlin `object`/`UseCase`)로 둡니다. 화면 버튼과 음성 툴이 **같은 함수**를 부르면 두 경로의 동작이 어긋나지 않습니다.

## 9. 버전 관리

- 툴 이름 변경, 인자 삭제, 의미 변경은 **메이저** 버전을 올립니다(앱과 음성 비서를 함께 바꿔야 함).
- 툴 추가, 선택 인자 추가, 반환 필드 추가는 **마이너** 버전을 올립니다(기존 쪽이 그대로 동작함).
- 앱은 `get_context` 응답에 `contract_version`을 넣는 것을 권장합니다(v0.2에서 필수로 할 예정).

## 10. 아직 정하지 않은 것 (v0.2 후보)

- 음성 비서의 GUI 조작 범위: 7절의 화면(주소·결제·로그아웃·초기화)은 GUI로도 누르지 못하게 하는 화면 차단 목록이 필요합니다.
- 어르신 역할의 응답 길이와 TTS 속도 기준(헌장 U6·U7의 음성판).
- 메뉴 이름 검색 `find_menu(query)`: 주문할 수 없는 메뉴도 찾아서 이유(알레르기, 판매 중지, 건강 기준)를 말해 주기. `MENU_NOT_ALLOWED` 오류에 메뉴 이름과 이유 넣기. 평가 과제를 만들며 발견했습니다([eval.md](eval.md)).
- 두 기기 연동(서버)이 생기면 `get_order_status`의 실시간 알림을 음성으로 먼저 말해 주기(push).
