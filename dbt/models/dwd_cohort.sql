-- dwd_cohort：研究队列明细层（S3 论文轨 · 架构书 §六「论文轨」）
-- 输入 = 研究轨 raw 不可变批次（var: raw_csv，由 land_file 落地的批次文件）
-- 输出 = 队列明细表（participant_id / arm / converted / 准标识符列），
--        供论文轨断言闸门、组计数（plugins/research/inputs.py）、
--        k-匿名检查（core/privacy/kanonymity.py）使用。
-- 声明式：本模型不包含任何轨道路径字面量（路径经 var 注入；C-4）。
{{ config(materialized='table') }}

SELECT * FROM read_csv('{{ var("raw_csv") }}', header=true, sample_size=-1, strict_mode=false)
