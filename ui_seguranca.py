"""Tela de segurança e backup: backup manual, código de recuperação e reset do admin."""

from __future__ import annotations

import time
from collections.abc import Callable

import flet as ft

import acesso
import backup
import db
from logs import log
from settings import AppSettings
from ui_base import (
    BORDA,
    TEXTO,
    TEXTO_SUAVE,
    VERDE,
    VERDE_SUAVE,
    abrir_dialogo,
    estilo_campo,
    fechar_dialogo,
    mostrar_snack,
)


OCIOSIDADE_GERENTE = 10 * 60.0  # o acesso de gerência expira após 10 min sem uso


def _texto_copiavel(valor: str) -> ft.Control:
    return ft.Container(
        bgcolor=VERDE_SUAVE,
        border_radius=12,
        padding=ft.Padding.symmetric(horizontal=16, vertical=12),
        content=ft.Text(
            valor,
            selectable=True,
            size=18,
            weight=ft.FontWeight.W_700,
            color=TEXTO,
        ),
    )


def _rotulo(texto: str) -> ft.Text:
    return ft.Text(texto, size=13, color=TEXTO_SUAVE)


class SegurancaMixin:
    page: ft.Page
    config: AppSettings

    # ------------------------------------------------------------ primeiro uso

    def mostrar_primeiros_passos(self, senha: str | None, codigo: str | None) -> None:
        itens: list[ft.Control] = [
            ft.Text(
                "Anote estas informações agora. Elas não serão mostradas novamente "
                "sem uma confirmação de administrador.",
                size=13,
                color=TEXTO,
            )
        ]
        if senha:
            itens += [
                ft.Divider(height=1, color=BORDA),
                ft.Text("Acesso remoto - usuário: admin", weight=ft.FontWeight.W_600, color=TEXTO),
                _texto_copiavel(senha),
                _rotulo("A troca desta senha é exigida no primeiro acesso remoto."),
            ]
        if codigo:
            itens += [
                ft.Divider(height=1, color=BORDA),
                ft.Text("Código de recuperação dos backups", weight=ft.FontWeight.W_600, color=TEXTO),
                _texto_copiavel(codigo),
                _rotulo(
                    "Guarde fora deste computador (cofre, gerência). Sem ele não é possível "
                    "abrir os backups em outro computador."
                ),
            ]

        dialogo = abrir_dialogo(
            self.page,
            "Guarde estas informações",
            ft.Column(itens, tight=True, spacing=10, width=480, scroll=ft.ScrollMode.AUTO),
            confirmar="Já anotei",
            ao_confirmar=lambda _: fechar_dialogo(dialogo),
            cancelar="Fechar",
        )

    # ------------------------------------------------- acesso de gerente/admin

    _gerente: acesso.Usuario | None = None
    _gerente_ultimo_uso: float = 0.0

    def _gerente_ativo(self) -> acesso.Usuario | None:
        """Gerente/administrador identificado neste computador, ou None.

        A identificação expira por inatividade e o perfil é relido do banco, então
        rebaixar ou excluir a conta tira o acesso na hora.
        """
        usuario = self._gerente
        if usuario is None:
            return None
        if time.monotonic() - self._gerente_ultimo_uso > OCIOSIDADE_GERENTE:
            self._gerente = None
            return None
        atual = acesso.obter_usuario(usuario.id)
        if atual is None or not atual.pode("configurar"):
            self._gerente = None
            return None
        self._gerente = atual
        self._gerente_ultimo_uso = time.monotonic()
        return atual

    def _exigir_gerente(self, depois: Callable[[], None]) -> None:
        """Executa `depois` se houver gerente/admin identificado; senão pede o login."""
        if self._gerente_ativo() is not None:
            depois()
            return
        self._atualizar_menu_remoto()
        self.entrar_modo_gerente(depois)

    def abrir_configuracao_protegida(self) -> None:
        self._exigir_gerente(self.abrir_dialogo_configuracao)

    def abrir_seguranca_protegida(self) -> None:
        self._exigir_gerente(self.abrir_dialogo_seguranca)

    def sair_modo_gerente(self) -> None:
        usuario = self._gerente
        self._gerente = None
        if usuario is not None:
            acesso.registrar_evento(
                "logout", "Encerrou o acesso de gerência neste computador.",
                usuario=usuario.usuario, perfil=usuario.perfil, ip="local",
            )
        self._atualizar_menu_remoto()
        mostrar_snack(self.page, "Acesso de gerência encerrado.")

    def entrar_modo_gerente(self, depois: Callable[[], None] | None = None) -> None:
        usuario = ft.TextField(label="Usuário", autofocus=True, **estilo_campo(380))
        senha = ft.TextField(
            label="Senha",
            password=True,
            can_reveal_password=True,
            max_length=128,
            **estilo_campo(380),
        )

        def confirmar(_) -> None:
            try:
                autenticado = acesso.autenticar(usuario.value or "", senha.value or "", "local")
            except acesso.LoginBloqueado as exc:
                mostrar_snack(self.page, str(exc))
                return
            if autenticado is None:
                mostrar_snack(self.page, "Usuário ou senha incorretos.")
                return
            if not autenticado.pode("configurar"):
                acesso.registrar_evento(
                    "login_falhou", "Perfil sem permissão para os menus de gerência.",
                    usuario=autenticado.usuario, perfil=autenticado.perfil, ip="local",
                )
                mostrar_snack(self.page, "Somente gerentes e administradores têm acesso a este menu.")
                return
            self._gerente = autenticado
            self._gerente_ultimo_uso = time.monotonic()
            fechar_dialogo(dialogo)
            self._atualizar_menu_remoto()
            if depois is not None:
                depois()

        senha.on_submit = confirmar
        dialogo = abrir_dialogo(
            self.page,
            "Acesso de gerente ou administrador",
            ft.Column(
                [
                    _rotulo(
                        "Impressora, rede, segurança e backup só podem ser usados por "
                        "gerentes e administradores."
                    ),
                    usuario,
                    senha,
                ],
                tight=True,
                spacing=12,
                width=400,
            ),
            confirmar="Entrar",
            ao_confirmar=confirmar,
        )

    # --------------------------------------------------------------- principal

    def abrir_dialogo_seguranca(self) -> None:
        if self._gerente_ativo() is None:  # defesa extra: nunca abre sem identificação
            self._exigir_gerente(self.abrir_dialogo_seguranca)
            return
        estado = backup.estado()
        ultimo = estado["ultimo"].strftime("%d/%m/%Y %H:%M") if estado["ultimo"] else "nenhum ainda"
        integro, detalhe = db.verificar_integridade()
        log_ok, log_msg = acesso.verificar_log()

        def linha(icone, texto: str, ok: bool = True) -> ft.Control:
            return ft.Row(
                [
                    ft.Icon(icone, size=18, color=VERDE if ok else "#B3261E"),
                    ft.Text(texto, size=13, color=TEXTO, expand=True),
                ],
                spacing=8,
            )

        def botao(rotulo: str, icone, acao) -> ft.Control:
            return ft.OutlinedButton(
                rotulo,
                icon=icone,
                on_click=lambda _: (fechar_dialogo(dialogo), acao()),
                width=460,
            )

        conteudo = ft.Column(
            [
                linha(
                    ft.Icons.BACKUP,
                    f"Último backup: {ultimo} ({estado['quantidade']} guardado(s))",
                    estado["ultimo"] is not None,
                ),
                linha(
                    ft.Icons.FACT_CHECK,
                    "Banco de dados íntegro" if integro else f"Banco com problema: {detalhe}",
                    integro,
                ),
                linha(
                    ft.Icons.VERIFIED_USER,
                    "Registro de auditoria íntegro" if log_ok else f"Auditoria: {log_msg}",
                    log_ok,
                ),
                _rotulo(f"Pasta dos backups: {estado['pasta']}"),
                ft.Divider(height=1, color=BORDA),
                botao("Fazer backup agora", ft.Icons.BACKUP, self._backup_agora),
                botao("Ver código de recuperação", ft.Icons.KEY, self._pedir_credenciais_codigo),
                botao("Redefinir senha do administrador", ft.Icons.LOCK_RESET, self._pedir_codigo_reset),
            ],
            tight=True,
            spacing=10,
            width=480,
        )
        dialogo = abrir_dialogo(self.page, "Segurança e backup", conteudo, cancelar="Fechar")

    # ------------------------------------------------------------------ backup

    def _backup_agora(self) -> None:
        mostrar_snack(self.page, "Criando backup...")
        manter = self.config.backup_manter

        def trabalho() -> None:
            erro: Exception | None = None
            nome = ""
            try:
                nome = backup.criar_backup("manual", manter).name
                acesso.registrar_evento("backup", f"Backup manual criado: {nome}")
            except Exception as exc:
                erro = exc
                log("backup").exception("Falha no backup manual")

            async def concluir() -> None:
                if erro:
                    mostrar_snack(self.page, f"Falha ao criar o backup: {erro}")
                else:
                    mostrar_snack(self.page, f"Backup criado: {nome}")

            self.page.run_task(concluir)

        self.page.run_thread(trabalho)

    # -------------------------------------------------- código de recuperação

    def _pedir_credenciais_codigo(self) -> None:
        usuario = ft.TextField(label="Usuário administrador", autofocus=True, **estilo_campo(380))
        senha = ft.TextField(
            label="Senha",
            password=True,
            can_reveal_password=True,
            max_length=128,
            **estilo_campo(380),
        )

        def confirmar(_) -> None:
            try:
                autenticado = acesso.autenticar(usuario.value or "", senha.value or "", "local")
            except acesso.LoginBloqueado as exc:
                mostrar_snack(self.page, str(exc))
                return
            if autenticado is None or autenticado.perfil != acesso.PERFIL_ADMIN:
                mostrar_snack(self.page, "Usuário ou senha incorretos, ou sem permissão de administrador.")
                return
            fechar_dialogo(dialogo)
            acesso.registrar_evento(
                "backup", "Código de recuperação consultado.",
                usuario=autenticado.usuario, perfil=autenticado.perfil, ip="local",
            )
            self.mostrar_primeiros_passos(None, backup.codigo_recuperacao())

        senha.on_submit = confirmar
        dialogo = abrir_dialogo(
            self.page,
            "Confirme que você é administrador",
            ft.Column([usuario, senha], tight=True, spacing=12),
            confirmar="Continuar",
            ao_confirmar=confirmar,
        )

    # ---------------------------------------------------- reset da senha admin

    def _pedir_codigo_reset(self) -> None:
        codigo = ft.TextField(
            label="Código de recuperação",
            autofocus=True,
            max_length=80,
            **estilo_campo(420),
        )

        def confirmar(_) -> None:
            try:
                correto = backup.codigo_confere(codigo.value or "")
            except backup.ErroBackup as exc:
                mostrar_snack(self.page, str(exc))
                return
            if not correto:
                mostrar_snack(self.page, "Código de recuperação incorreto.")
                return
            fechar_dialogo(dialogo)
            nome, nova = acesso.redefinir_senha_admin()
            self.mostrar_primeiros_passos(nova, None)
            mostrar_snack(self.page, f"Senha do usuário {nome} redefinida.")

        codigo.on_submit = confirmar
        dialogo = abrir_dialogo(
            self.page,
            "Redefinir senha do administrador",
            ft.Column(
                [
                    _rotulo(
                        "Digite o código de recuperação que foi mostrado no primeiro uso. "
                        "Uma nova senha será gerada e a troca será exigida no próximo acesso."
                    ),
                    codigo,
                ],
                tight=True,
                spacing=12,
                width=460,
            ),
            confirmar="Redefinir",
            ao_confirmar=confirmar,
        )
