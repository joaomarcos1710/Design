# Controle de Atendimentos (versão em Python)

Programa que faz o mesmo que a planilha `controle_atendimentos.xlsx`, mas com tela
de cadastro, busca e painel. Ele roda **só no seu computador**: abre no navegador,
mas não está na internet.

- Um único arquivo: `atendimentos.py`.
- Usa apenas o Python padrão (3.8 ou mais novo). **Não precisa instalar nada.**
- Os dados ficam em `dados/atendimentos.db`, ao lado do programa.

## Como usar

1. Copie a pasta `app` para uma pasta autorizada pela empresa (por exemplo, dentro
   do OneDrive corporativo).
2. Abra o Prompt de Comando nessa pasta e rode:

   ```
   python atendimentos.py
   ```

   Em alguns computadores o comando é `py atendimentos.py`.
3. O navegador abre em `http://127.0.0.1:8765/`. Deixe a janela preta aberta
   enquanto usa; para encerrar, `Ctrl + C` nela.

Na primeira vez, vá em **Listas** e digite os seus indicadores (um por linha).

## Telas

| Tela | Para que serve |
|---|---|
| **Painel** | Números do dia (vencidas, a vencer, ações de hoje), as 15 pendências abertas há mais tempo e os quadros por status, produto, indicador, canal, tipo e mês |
| **Registro** | Todos os atendimentos, com busca e filtro. Botão **Concluir** fecha o caso com a data de hoje |
| **Novo atendimento** | Ficha de cadastro |
| **Listas** | Opções das listas suspensas, feriados e o limite do alerta |

## Regras (as mesmas da planilha)

- **Amarelo**: em aberto. **Vermelho**: prazo vencido; sem prazo, em aberto há mais
  dias úteis que o limite configurado em Listas. **Verde**: concluído.
- **Laranja** na data da próxima ação: é para hoje ou está atrasada.
- Dias são contados em **dias úteis** (segunda a sexta, menos os feriados cadastrados).
- CPF é conferido pelos dígitos verificadores e guardado sempre como `000.000.000-00`.
- Ao marcar **Concluído** sem data da resposta, o programa preenche com a data de hoje.

## Cuidados com os dados

- O arquivo `dados/atendimentos.db` contém dados de clientes e **não é criptografado**.
  Quem protege é a pasta onde ele está: mantenha-a em local autorizado pela empresa.
- A cada dia de uso o programa guarda uma cópia em `dados/backup` (as 10 mais recentes).
- **Exportar para Excel (CSV)** gera um arquivo com todos os dados: trate-o com o mesmo cuidado.
- O `.gitignore` do repositório impede que `dados/`, `*.db` e `*.csv` sejam enviados ao GitHub.

## Opções

```
python atendimentos.py --porta 9000       # usar outra porta
python atendimentos.py --sem-navegador    # não abrir o navegador sozinho
```

Para guardar o banco em outro lugar, defina a variável de ambiente `ATENDIMENTOS_DB`
com o caminho completo do arquivo.

## Limites conhecidos

- Não importa a planilha Excel: os atendimentos já lançados precisam ser redigitados.
- Um registro por atendimento, sem histórico de interações separado.
- Feito para uma pessoa usar por vez, no próprio computador.
