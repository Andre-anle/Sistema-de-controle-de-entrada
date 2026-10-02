# Sistema-de-etiqueta-hortifruti
Reformulação do sistema de etiqueta do CD Pavuna Hortifruti Natural da Terra

## Documentação

* [`docs/SEGURANCA.md`](docs/SEGURANCA.md) — controles de segurança, plano de manutenção preventiva (PMP), certificado HTTPS e limitações.
* [`docs/RECUPERACAO.md`](docs/RECUPERACAO.md) — backup, restauração e plano de recuperação (RPO/RTO).

## Desenvolvimento

```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements-dev.txt
.\venv\Scripts\python main.py                                         # executar
.\venv\Scripts\python -m unittest discover -s tests -t . -v           # testes (banco temporário)
```

Restaurar um backup (com o programa fechado): `EtiquetasHortifruti.exe --restaurar <arquivo.ehb> [código]`.
