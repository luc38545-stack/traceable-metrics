-- dwd_base：明细层（架构书 §02 L2/§04-L3）
-- 输入 = 本轨 raw 不可变批次（var: raw_csv，由 land_file 落地的批次文件）
-- 输出 = 明细表，供语义层契约视图引用。
-- 声明式：本模型不包含任何轨道路径字面量（路径经 var 注入）。
{{ config(materialized='table') }}

SELECT * FROM read_csv('{{ var("raw_csv") }}', header=true, sample_size=-1, strict_mode=false)
