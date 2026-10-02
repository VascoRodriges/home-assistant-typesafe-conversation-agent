# Quick start / Быстрый запуск

## Русский

Начиная с v0.4.0 есть готовый профиль **«Свет и датчики»**. Он не требует YAML,
сценариев или создания вспомогательных переключателей. Конфигурация конкретного
дома не входит в поставку: объекты, имена, псевдонимы и комнаты берутся из HA.

### Установка и первый запрос

1. В HACS добавьте этот репозиторий как пользовательскую интеграцию:
   `VascoRodriges/home-assistant-typesafe-conversation-agent`. Установите TypeSafe
   Conversation и перезапустите HA. Альтернатива: скопируйте каталог
   `custom_components/typesafe_conversation` в конфигурацию HA. Требуется HA 2026.5+.
2. **Настройки → Устройства и службы → Добавить интеграцию → TypeSafe Conversation**.
   Выберите **«Быстрый запуск: свет и датчики»**. Укажите имя и ключ OpenRouter
   либо уже установленную интеграцию OpenRouter; при выборе существующего источника
   поле ключа оставьте пустым. Ключ проверяется из сети HA; он не отправляется модели
   в тексте запроса. Не нужны отдельные ключи JEV и GPT.
3. **Настройки → Голосовые ассистенты → Доступные объекты**: откройте для Assist
   только нужные светильники `light.*` и числовые датчики `sensor.*` с единицей
   измерения. Устройства должны уже работать в HA. Назначьте понятные имена,
   псевдонимы и комнаты в обычных настройках HA. Интеграция не открывает объекты сама.
4. В настройках конвейера Assist выберите созданного агента TypeSafe Conversation.
   При проверке именно этого агента отключите **предпочтительную локальную обработку
   команд HA**: иначе встроенный обработчик может исполнить команду в обход профиля,
   его режима проверки и лимитов. Отдельный микрофон/STT/TTS для текстовых тестов не нужен.
5. Управление выключено по умолчанию. Его можно включить **одним переключателем**
   при создании профиля либо позже: запись интеграции → **Настроить → Безопасность
   и агент → Разрешить команды**. До этого профиль только показывает план.
6. Отправьте в Assist: «включи свет в комнате», «сделай эту лампу чуть ярче» или
   «какая температура в комнате?» Используйте свои реальные имена и псевдонимы.

Это готовые настройки, а не автоматическая установка устройств или выдача ключа.
В любом новом доме остаются обязательными действующие интеграции устройств,
оплата/ключ OpenRouter, доступ в Интернет и выбор агента Assist. При сетевой блокировке
проверьте соединение **с самого HA** и собственный разрешённый маршрут до OpenRouter;
адреса и сетевые правила чужого дома не поставляются.

### Что уже настроено

| Параметр | Начальное значение |
| --- | --- |
| Решения | `typesafe/jev-1.13` |
| Сложный план и общие ответы | `openai/gpt-4o-mini` |
| Предел на запрос / сутки UTC / месяц UTC | $0.035 / $0.25 / $3 |
| История числовых датчиков | Включена, до 7 дней; нужен HA Recorder |
| Интернет-поиск | Выключен; можно явно включить в маршрутизации |
| Управление устройствами | Выключено до явного разрешения владельца |
| Источники | Только свет и числовые датчики, открытые для Assist; до 100 объектов |

Простые подходящие команды и текущие показания разрешает один пакет JEV.
Сложные запросы используют планировщик и проверку JEV; ответы на общие вопросы —
отдельную модель ответов. Расход резервируется и сохраняется перед платным вызовом.
Цена/задержка не гарантированы. Начальные ценовые потолки в USD за миллион токенов:
JEV — 0.042 вход / 0 выход; GPT-4o mini — 0.15 вход / 0.60 выход. При изменении
провайдером цены вызов выше потолка отклоняется; проверьте актуальные тарифы и потолки.
Лимит ограничивает именно этот профиль, а не весь аккаунт OpenRouter.

### Область доступа и свет по комнатам

**Настроить → Свет и датчики: доступные объекты**:

- Пустой список означает все подходящие объекты, открытые для Assist.
- Непустой список ограничивает профиль выбранными объектами. Они всё равно должны
  быть открыты для Assist и доступны пользователю HA. Выбор в этом списке не выдаёт
  разрешение закрытому объекту.
- По имени или псевдониму можно выбрать отдельную лампу. Команда по комнате
  применяется ко всем выбранным светильникам этой комнаты. Профиль не угадывает,
  какой из них «основной», настольный или контурный: для основного света ограничьте
  список соответствующими лампами, либо используйте расширенный каталог с ролями.

Относительная яркость использует текущее состояние, обычный шаг — 10 процентных
пунктов. «Чуть ярче» для выключенной диммируемой лампы включает её на 10%, а
«темнее» не будит уже выключенную лампу. Тёплый/холодный белый и цвет доступны только
при поддержке светильником; неподдерживаемый атрибут не игнорируется молча.
Цветовая температура ограничивается пределами устройства; шаг теплее/холоднее — 250 K.
Перед началом проверяется вся цепочка: цели, права и функции ламп. Ошибка останавливает
оставшиеся действия без повторов. Успех означает принятие команды службой HA, а не
независимое подтверждение физического состояния. Уже выполненные действия не откатываются.

Текущие датчики читаются из HA. Для вопроса «а вчера в это время?» сохраняйте тот же
диалог; нужны записи Recorder, достаточный срок хранения и неизменная единица измерения.
Сырые исторические записи не отправляются модели. В модель идут запрос, ограниченный
диалог, подписи выбранных объектов и необходимые текущие состояния.

### Базовый, расширенный и исходный режимы

Базовый профиль не исполняет произвольные службы/скрипты, не включает ресивер,
не запускает уборку, не управляет замками и не создаёт отложенные задания.
Для Music Assistant, комнатных ролей и домашних сценариев используется отдельный
[расширенный YAML-профиль](openrouter-fork.md). Его пользователь определяет для своего
дома; привязок конкретного владельца в пакете нет. Обновление **не переводит** уже
установленный расширенный профиль в базовый и не меняет его бюджет или модели.

Исходный режим **«Нативные intents»** сохранён для совместимости с оригинальным
проектом; он не использует локальный бюджетный ограничитель базового/расширенного
профиля. Не выбирайте его, рассчитывая на описанные выше лимиты.

Раздел **Настроить → Инструкция по настройке** содержит краткие шаги прямо в HA.
Изменения интерфейса переопределяют исходные настройки, не переписывая YAML.
**Вернуть исходные настройки** требует подтверждения и удаляет переопределения;
расходы сохраняются. Ключ меняется через повторную авторизацию своего источника.

Иконки поставляются локально в `brand/`, в обычном/тёмном варианте и 1×/2×.
HA 2026.3+ поддерживает локальные изображения интеграций; обновите страницу после
установки. В отдельных версиях HACS собственная карточка может всё ещё ожидать
иконку из глобального реестра; это не мешает иконке в HA.

## English

The v0.4.0 **Lights and sensors** profile needs no YAML, scripts or execution helper.
Install through HACS as a custom integration repository, restart HA, then add
**TypeSafe Conversation → Quick start**. Supply an OpenRouter key or select an
existing OpenRouter entry (leave the key blank). Expose the desired lights and
numeric sensors to Assist, assign names/aliases/areas, and select the new agent
in your Assist pipeline. Turn off preferred local command handling when testing
the agent's own preview/scope safeguards. Device commands default to off; enable
them with the explicit permission toggle during setup or in **Configure → Safety**.

Defaults: pinned Jev 1.13 decisions, GPT-4o mini planning/answers, $0.035 per request,
$0.25 per UTC day and $3 per UTC month, read-only Recorder history up to seven days,
internet search off. All paid model calls use persisted budget admission; native
upstream mode does not. Prices are ceilings, not a guarantee; inspect them when
provider pricing changes. You still need working HA devices, credentials and network
access. This is a ready-made profile, not automatic device/credential installation.

**Configure → Entity scope**: empty selects all eligible Assist-exposed entities
(up to 100), nonempty is their intersection with your explicit selection. HA user
permissions still apply. Room lighting means all selected lights in that area;
select primary fixtures only if that is what room commands should operate. Relative
brightness uses the current state (default step 10 percentage points); brighter
while off starts at 10%, dimmer while off does not wake the light. Features are
checked before any action. HA service acceptance is not independent physical
verification; a later failure does not roll back completed actions.

The package contains no household mappings, personal entity IDs, credentials or
live traces. Media, cleaning, schedules and custom workflows require the separate
[advanced profile](openrouter-fork.md); existing profiles are not migrated automatically.
The built-in **Configure → Setup guide** explains these steps in HA. Local light/dark
brand assets are included; some HACS versions may still show their own CDN placeholder.

Original integration: **Sofiane Ghadab (@the-sof)**, MIT. Public fork and these
additions: **VascoRodriges**. This is not an official HA or TypeSafe release.
