"""
Patch script: replaces the broken USERS JS object + synchronous openDetail()
in admin.html with an async version that fetches /admin/users/{userId}.

Root cause: Jinja2 does NOT recognize '{ { expr } }' (space between braces).
Those lines render literally, producing a JavaScript SyntaxError that breaks
the entire <script> block — including confirmDeleteUser and executeDeleteUser.

Fix: remove the USERS embedded object entirely; rewrite openDetail as an async
function that requests GET /admin/users/{userId} (new backend endpoint).
"""

import sys
import os

TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "templates", "admin.html"
)

NEW_OPEN_DETAIL = r"""        async function openDetail(userId) {
            const modal = document.getElementById('detail-modal');
            const content = document.getElementById('detail-content');
            modal.style.display = 'flex';
            document.body.style.overflow = 'hidden';
            content.innerHTML = '<div class="flex items-center justify-center py-12"><div class="w-8 h-8 border-2 border-blue-400 border-t-transparent rounded-full animate-spin"></div></div>';

            let u;
            try {
                const res = await fetch('/admin/users/' + userId, { credentials: 'same-origin' });
                if (!res.ok) {
                    content.innerHTML = '<p class="text-red-400 text-sm text-center py-8">Erro ao carregar dados do usu\u00e1rio.</p>';
                    return;
                }
                u = await res.json();
            } catch (e) {
                content.innerHTML = '<p class="text-red-400 text-sm text-center py-8">Erro de conex\u00e3o.</p>';
                return;
            }

            const badge = (ok, label) => ok
                ? `<span class="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-green-900/50 text-green-400 font-medium"><svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>${label}</span>`
                : `<span class="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-yellow-900/40 text-yellow-400 font-medium"><svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>${label} pendente</span>`;

            const field = (label, value) => `
                <div>
                    <p class="text-xs text-gray-500 uppercase tracking-wide mb-1 font-medium">${label}</p>
                    <p class="text-white font-medium text-sm break-words">${value || '<span class="text-gray-600 font-normal italic">n\u00e3o informado</span>'}</p>
                </div>`;

            const fmtBRL = v => 'R$\u00a0' + Number(v).toFixed(2).replace('.', ',');
            const addressFull = [u.address_street, u.address_number, u.address_complement].filter(Boolean).join(', ');

            const txTypeLabel = t => t.type === 'ENVIADO'
                ? '<span class="text-red-400 text-xs font-medium">Enviado</span>'
                : '<span class="text-green-400 text-xs font-medium">Recebido</span>';

            const recentRows = u.recent_pix.length
                ? u.recent_pix.map(t => `
                    <tr class="border-b border-gray-700/50 last:border-0">
                        <td class="py-2 pr-3">${txTypeLabel(t)}</td>
                        <td class="py-2 pr-3 tabular-nums text-white text-xs">${fmtBRL(t.value)}</td>
                        <td class="py-2 pr-3 text-gray-400 text-xs">${t.status}</td>
                        <td class="py-2 text-gray-500 text-xs">${t.created_at}</td>
                    </tr>`).join('')
                : '<tr><td colspan="4" class="py-4 text-center text-gray-600 text-xs italic">Nenhuma transa\u00e7\u00e3o PIX</td></tr>';

            content.innerHTML = `
                <div class="flex flex-wrap gap-2 mb-4">
                    ${badge(u.document_verified, 'Documento')}
                    ${badge(u.email_verified, 'E-mail')}
                    ${u.is_active
                        ? '<span class="px-2 py-1 rounded-full text-xs bg-green-900/50 text-green-400 font-medium">Conta ativa</span>'
                        : '<span class="px-2 py-1 rounded-full text-xs bg-red-900/50 text-red-400 font-medium">Conta inativa</span>'}
                    ${u.is_admin ? '<span class="px-2 py-1 rounded-full text-xs bg-yellow-900/40 text-yellow-300 font-medium">Admin</span>' : ''}
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 bg-gray-800/40 rounded-xl p-4">
                    ${field('Nome completo', u.name)}
                    ${field('CPF / CNPJ', '<span class="font-mono">' + u.cpf_cnpj + '</span>')}
                    ${field('E-mail', u.email)}
                    ${field('Telefone', u.phone)}
                </div>

                <div class="bg-gray-800/40 rounded-xl p-4">
                    <p class="text-xs text-gray-500 uppercase tracking-wide mb-3 font-medium">Endere\u00e7o</p>
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        ${field('Logradouro', addressFull)}
                        ${field('Cidade / UF', u.address_city ? u.address_city + ' / ' + u.address_state : '')}
                        ${field('CEP', u.address_zip ? '<span class="font-mono">' + u.address_zip + '</span>' : '')}
                    </div>
                </div>

                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-gray-800/40 rounded-xl p-4">
                    <div>
                        <p class="text-xs text-gray-500 uppercase tracking-wide mb-1 font-medium">Saldo</p>
                        <p class="text-white font-bold text-base tabular-nums">${fmtBRL(u.balance)}</p>
                    </div>
                    <div>
                        <p class="text-xs text-gray-500 uppercase tracking-wide mb-1 font-medium">PIX Enviados</p>
                        <p class="text-red-300 font-semibold text-sm tabular-nums">${u.stats.pix_sent_count}x ${fmtBRL(u.stats.pix_sent_total)}</p>
                    </div>
                    <div>
                        <p class="text-xs text-gray-500 uppercase tracking-wide mb-1 font-medium">PIX Recebidos</p>
                        <p class="text-green-300 font-semibold text-sm tabular-nums">${u.stats.pix_received_count}x ${fmtBRL(u.stats.pix_received_total)}</p>
                    </div>
                    <div>
                        ${field('Cadastro em', u.created_at)}
                    </div>
                </div>

                <div class="bg-gray-800/40 rounded-xl p-4">
                    <p class="text-xs text-gray-500 uppercase tracking-wide mb-3 font-medium">\u00daltimas transa\u00e7\u00f5es PIX</p>
                    <table class="w-full">
                        <thead>
                            <tr class="border-b border-gray-700">
                                <th class="pb-2 text-left text-xs text-gray-500 font-medium">Tipo</th>
                                <th class="pb-2 text-left text-xs text-gray-500 font-medium">Valor</th>
                                <th class="pb-2 text-left text-xs text-gray-500 font-medium">Status</th>
                                <th class="pb-2 text-left text-xs text-gray-500 font-medium">Data</th>
                            </tr>
                        </thead>
                        <tbody>${recentRows}</tbody>
                    </table>
                </div>

                <div class="flex flex-col sm:flex-row gap-2.5 pt-1">
                    <button onclick="toggleActive('${userId}', ${!u.is_active})"
                        class="tap-target flex-1 py-2.5 rounded-xl text-sm font-semibold transition-colors flex items-center justify-center gap-2 ${u.is_active ? 'bg-red-950/60 hover:bg-red-950 text-red-300' : 'bg-green-950/60 hover:bg-green-950 text-green-300'}">
                        ${u.is_active
                            ? '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/></svg> Suspender conta'
                            : '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg> Reativar conta'}
                    </button>
                    <button onclick="closeDetail()" class="tap-target flex-1 py-2.5 rounded-xl text-sm font-semibold bg-gray-800 hover:bg-gray-700 active:bg-gray-600 text-gray-300 transition-colors">
                        Fechar
                    </button>
                </div>`;
        }

"""

START_MARKER = "        const USERS = {"
END_MARKER = "        function closeDetail()"


def main():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    start_idx = content.find(START_MARKER)
    end_idx = content.find(END_MARKER)

    if start_idx == -1:
        print("ERROR: START_MARKER not found. Already patched or file changed.")
        sys.exit(1)
    if end_idx == -1:
        print("ERROR: END_MARKER not found in file.")
        sys.exit(1)
    if start_idx >= end_idx:
        print("ERROR: START_MARKER appears after END_MARKER — unexpected structure.")
        sys.exit(1)

    replaced = content[:start_idx] + NEW_OPEN_DETAIL + content[end_idx:]

    with open(TEMPLATE_PATH, "w", encoding="utf-8") as f:
        f.write(replaced)

    print(f"OK: replaced {end_idx - start_idx} bytes. New block is {len(NEW_OPEN_DETAIL)} chars.")


if __name__ == "__main__":
    main()
