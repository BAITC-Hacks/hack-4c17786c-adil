# Исходный пакет участника

`beeline_case_participants.zip` — неизменённый архив из [материалов кейса](https://drive.google.com/file/d/1cQUKtE_cm9TVXgzpcFwQYYUpmuFaJHHT/view), загруженный 23 сентября 2026 года. Все данные синтетические.

SHA-256: `df1d955fb97816ff6de8ceb142ed589915d969f43f840a650dcdfbe12734f20b`.

Архив хранит все исходные материалы, включая CSV и PDF. Данные восстанавливаются командой `python setup_case.py`: она проверяет контрольную сумму и не перезаписывает изменённые CSV. Интернет не нужен.

В корне репозитория также сохранены без изменений исходные `environment.py`, `mock_environment.py`, `scoring_core.py`, `local_eval.py`, `make_submission.py`, `agent_template.py`, `PARTICIPANT_GUIDE.md`. Агент не импортирует оценщик или скрытую mock-модель; публичная фабрика среды используется только скриптами проверки и демонстрации.
