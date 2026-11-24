# Transcoding de vídeo em ARM e x86 na AWS EC2

Código e dados do Trabalho de Conclusão de Curso **"Comparação de Desempenho e Custo de Transcoding de Vídeo entre Arquiteturas ARM e x86 em Ambientes de Nuvem"**, de João Madeira Carneiro Braga de Freitas, com orientação de Cristiano Neves Rodrigues, no curso de Ciência da Computação da PUC Minas.

O experimento compara três instâncias EC2 de 4 vCPUs da geração c7:
- `c7g.xlarge` (AWS Graviton3, ARM);
- `c7i.xlarge` (Intel Sapphire Rapids);
- `c7a.xlarge` (AMD Genoa).

Cada uma transcodifica com FFmpeg em H.264 (`libx264`), H.265 (`libx265`) e AV1 (`libsvtav1`). As medidas são tempo, *throughput*, utilização de CPU, contadores de *hardware* (PMU), qualidade (VMAF e SSIM) e custo.

## Estrutura

| Pasta | Conteúdo |
|---|---|
| `dados/` | Os dados consolidados do experimento, em Parquet |
| `analysis/` | Consolidação dos resultados, tabelas de qualidade, *gate* de validação e experimentos complementares |
| `config/` | A definição de cada lançamento: campanha, piloto e os três experimentos complementares |
| `orchestrator/` | O orquestrador, que lança as instâncias, distribui os cenários, vigia a execução e retoma falhas |
| `encode/` | O que roda em cada instância de medição: um cenário por vez, com `perf`, `pidstat` e `time` |
| `judge/` | A medição de qualidade (VMAF e SSIM), numa instância separada |
| `masters/` | A preparação dos vídeos de entrada a partir das fontes originais |
| `docker/` | A imagem com FFmpeg, os três *encoders* e o `libvmaf`, com versões fixadas |
| `infra/` | A infraestrutura na AWS, em Terraform |
| `smoke/` | Testes de aceitação de ponta a ponta, com *shims* no lugar de `aws`, `ffmpeg` e `perf` |

## Dados

| Arquivo | Linhas | O que é |
|---|---|---|
| `dados/campanha/campaign.parquet` | 810 | A campanha principal: 3 instâncias × 3 codecs × 9 pares de resolução × 2 vídeos × 5 replicações |
| `dados/campanha/quality_*.parquet` | | A qualidade das saídas da campanha (VMAF e SSIM), por *bitstream* e por grupo |
| `dados/piloto/` | 90 | O piloto que validou o procedimento antes da campanha, com a qualidade dele |
| `dados/complementares/topologia/` | 540 | A `c7i.2xlarge` com e sem SMT, para isolar o efeito do SMT |
| `dados/complementares/repetibilidade/` | 135 | Três *hosts* de cada tipo, para medir a variação entre máquinas |
| `dados/complementares/contadores/` | 15 | A `c7g.xlarge` medindo só `cycles` e `instructions` entre os eventos de *hardware*, para evitar a multiplexação |

Cada linha dos Parquets de execução é uma execução: o cenário, a instância, o tempo, a CPU, os contadores e o `output_sha256` do vídeo gerado. Os vídeos de saída e os registros brutos de cada execução não estão no repositório, por causa do tamanho (cerca de 3 GB).

A coluna `commit` registra a versão do código que produziu cada linha, num repositório de desenvolvimento anterior a este. Esse repositório não é público, então os SHAs da coluna (e os citados nos READMEs de `tests/fixtures/`) identificam a versão, mas não podem ser abertos aqui. Os experimentos complementares rodaram com o código deste repositório; desde então, mudaram só comentários, o texto de algumas mensagens, o endereço deste repositório e o nome de uma variável do Terraform (`ssh_allowed_cidr`); o comportamento é o mesmo. A campanha e o piloto rodaram com versões anteriores. Entre a versão da campanha e esta, a imagem Docker, o comando de cada cenário (`encode/run_scenario.sh`) e a configuração do experimento (`config/experiment.toml`) não mudaram, fora os comentários. As mudanças estão no orquestrador, na análise, na infraestrutura e nos scripts que coordenam cada instância, para permitir os experimentos complementares.

## Reproduzir a análise

Basta Python 3.12; não é preciso conta na AWS.

```bash
python3.12 -m venv .venv-analysis
.venv-analysis/bin/pip install -r analysis/requirements-dev.txt
```

As tabelas dos experimentos complementares, que comparam cada um com a campanha:

```bash
.venv-analysis/bin/python analysis/complementares.py \
    --campaign dados/campanha/campaign.parquet \
    --pilot dados/piloto/pilot.parquet \
    --topologia dados/complementares/topologia/topologia.parquet \
    --repetibilidade dados/complementares/repetibilidade/repetibilidade.parquet \
    --contadores dados/complementares/contadores/contadores.parquet
```

O *gate* de validação da campanha (falhas, contadores, frames, ocupação de CPU e equivalência de qualidade) e o custo por cenário:

```bash
.venv-analysis/bin/python analysis/gate.py \
    --parquet dados/campanha/campaign.parquet \
    --config config/experiment.toml \
    --prices analysis/prices.toml --covers campanha \
    --quality dados/campanha/quality_groups.parquet
```

Os preços por hora usados no custo estão em `analysis/prices.toml`, com a data de cada consulta à AWS.

## Testes

Cada parte tem o seu ambiente, como na CI (`.github/workflows/ci.yml`):

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r orchestrator/requirements-dev.txt
.venv/bin/python -m pytest orchestrator/
```

```bash
.venv-analysis/bin/python -m pytest analysis/
```

```bash
python3.12 -m venv .venv-smoke && .venv-smoke/bin/pip install -r smoke/requirements-dev.txt
.venv-smoke/bin/python -m pytest smoke/
```

## Reproduzir o experimento na AWS

**Atenção: isto tem custo.** A campanha completa levou cerca de quatro dias, com o orquestrador ligado o tempo todo. O Terraform cria um orçamento de US$ 150 com alerta por e-mail.

O orquestrador roda numa instância `t3.micro`. Ela clona este repositório do GitHub (`infra/compute/instance.tf` e `orchestrator/instance_launch.py`), e as instâncias de medição clonam o mesmo *commit*. Para rodar um *fork*, troque o endereço nesses dois arquivos.

**Pré-requisitos:**
- Terraform 1.15.8, a versão fixada em `infra/*/versions.tf`;
- AWS CLI v2, com credenciais da conta;
- a região `us-east-1`, onde valem as AMIs fixadas e os preços consultados.

**Cota de vCPU.** A cota de instâncias On-Demand padrão da conta precisa comportar o que estiver rodando ao mesmo tempo:
- a campanha usa 14 vCPUs (três instâncias `xlarge` e o orquestrador);
- o juiz de qualidade usa 16 (`c7i.4xlarge`);
- os três experimentos complementares rodando juntos chegam a 58.

Contas novas costumam ter uma cota menor; o aumento é pedido no Service Quotas.

1. **Bucket do estado do Terraform**, criado uma vez, à mão, com versionamento:

   ```bash
   aws s3api create-bucket --bucket <state-bucket> --region us-east-1
   aws s3api put-bucket-versioning --bucket <state-bucket> --versioning-configuration Status=Enabled
   ```

2. **Buckets dos experimentos**:

   ```bash
   terraform -chdir=infra/storage init -backend-config="bucket=<state-bucket>"
   terraform -chdir=infra/storage apply
   ```

3. **Rede, permissões e instância do orquestrador.** Este passo pede três variáveis: `state_bucket`, `ssh_allowed_cidr` (o seu IP público em `/32`) e `budget_notification_email`.

   ```bash
   terraform -chdir=infra/compute init -backend-config="bucket=<state-bucket>"
   terraform -chdir=infra/compute apply
   ```

4. **Acesso ao orquestrador:**

   ```bash
   aws ssm get-parameter --region us-east-1 \
       --name /transcoding-bench/orchestrator/ssh-private-key \
       --with-decryption --query 'Parameter.Value' --output text > ~/.ssh/transcoding-bench.pem
   chmod 600 ~/.ssh/transcoding-bench.pem
   ssh -i ~/.ssh/transcoding-bench.pem ubuntu@"$(terraform -chdir=infra/compute output -raw orchestrator_public_ip)"
   ```

5. **Na instância do orquestrador**, dentro de `tmux` e a partir de `~/transcoding-bench`, com `python` sendo o do `.venv`:

   ```bash
   python orchestrator/orchestrator.py --infra ~/work/infra.json prepare-masters
   python orchestrator/orchestrator.py --infra ~/work/infra.json preflight --instance-type c7g.xlarge
   python orchestrator/orchestrator.py --infra ~/work/infra.json run --config config/experiment.toml --bucket <bucket-da-campanha>
   python orchestrator/orchestrator.py --infra ~/work/infra.json judge --config config/experiment.toml --bucket <bucket-da-campanha> --plan <plano>
   ```

   Os passos são:
   - `prepare-masters` prepara os vídeos de entrada.
   - `preflight` confere cada tipo de instância e o juiz de qualidade.
   - `run` executa a campanha e vigia até o fim; se faltar alguma execução, `orchestrator/resume.py` retoma o que faltou.
   - `judge` mede a qualidade. O plano dele vem de `orchestrator/quality_triage.py`.

   Os experimentos complementares são o mesmo `run`, com `config/topologia.toml`, `config/repetibilidade.toml` e `config/contadores.toml`, cada um no seu bucket e no seu diretório de trabalho. Cada subcomando aceita `--help`.

6. **Consolidação**, de volta à máquina local:

   ```bash
   aws s3 sync s3://<bucket-da-campanha>/runs/ runs/
   .venv-analysis/bin/python analysis/consolidate.py --runs runs/ --config config/experiment.toml --out campaign.parquet
   aws s3 sync s3://<bucket-da-campanha>/quality/ quality/
   .venv-analysis/bin/python analysis/quality.py --results quality/results --config config/experiment.toml \
       --outputs quality_outputs.parquet --groups quality_groups.parquet
   ```

7. **Ao terminar**, `terraform -chdir=infra/compute destroy`. Os buckets do `infra/storage` não têm `force_destroy`: precisam ser esvaziados antes do `destroy`.

## Licença

O código está sob a licença MIT (`LICENSE`). Os dados em `dados/` estão sob a CC BY 4.0 (`dados/LICENSE`).
