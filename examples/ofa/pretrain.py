import argparse
import os
from types import SimpleNamespace

import torch
from torchmetrics import AUROC, Accuracy

import utils
from gp.lightning.data_template import DataModule
from gp.lightning.metric import (
    flat_binary_func,
    EvalKit,
)
from gp.lightning.module_template import ExpConfig
from gp.lightning.training import lightning_fit
from gp.utils.utils import (
    load_yaml,
    combine_dict,
    merge_mod,
    setup_exp,
    set_random_seed,
)
from lightning_model import GraphPredLightning
from models.model import BinGraphModel, BinGraphAttModel
from models.model import PyGRGCNEdge
from task_constructor import UnifiedTaskConstructor
from utils import (
    SentenceEncoder,
    MultiApr,
    MultiAuc,
)


# os.environ["CUDA_LAUNCH_BLOCKING"]="1"

def main(params):
    """
    0. Check GPU setting.
    """
    device, gpu_ids = utils.get_available_devices()
    gpu_size = len(gpu_ids)

    """
    1. Initiate task constructor.
    """
    encoder = SentenceEncoder(params.llm_name, batch_size=params.llm_b_size)

    task_config_lookup = load_yaml(
        os.path.join(os.path.dirname(__file__), "configs", "task_config.yaml")
    )
    data_config_lookup = load_yaml(os.path.join(os.path.dirname(__file__), "configs", "data_config.yaml"))

    if isinstance(params.task_names, str):
        task_names = [a.strip() for a in params.task_names.split(",")]
    else:
        task_names = params.task_names

    tasks = UnifiedTaskConstructor(
        task_names,
        params.load_texts,
        encoder,
        task_config_lookup,
        data_config_lookup,
        batch_size=params.batch_size,
        sample_size=params.train_sample_size,
    )
    val_task_index_lst, val_pool_mode = tasks.construct_exp()

    # remove llm model
    if encoder is not None:
        encoder.flush_model()

    """
    2. Load model 
    """
    out_dim = params.emb_dim + (params.rwpe if params.rwpe is not None else 0)

    gnn = PyGRGCNEdge(
        params.num_layers,
        5,
        out_dim,
        out_dim,
        drop_ratio=params.dropout,
        JK=params.JK,
    )

    bin_model = BinGraphAttModel if params.JK == "none" else BinGraphModel
    model = bin_model(model=gnn, llm_name=params.llm_name, outdim=out_dim, task_dim=1,
                      add_rwpe=params.rwpe, dropout=params.dropout)

    """
    3. Construct datasets and lightning datamodule.
    """

    if hasattr(params, "d_multiple"):
        if isinstance(params.d_multiple, str):
            data_multiple = [float(a) for a in params.d_multiple.split(",")]
        else:
            data_multiple = params.d_multiple
    else:
        data_multiple = [1]

    if hasattr(params, "d_min_ratio"):
        if isinstance(params.d_min_ratio, str):
            min_ratio = [float(a) for a in params.d_min_ratio.split(",")]
        else:
            min_ratio = params.d_min_ratio
    else:
        min_ratio = [1]


    train_data = tasks.make_train_data(data_multiple, min_ratio, data_val_index=val_task_index_lst)

    text_dataset = tasks.make_full_dm_list(
        data_multiple, min_ratio, train_data
    )
    params.datamodule = DataModule(
        text_dataset, gpu_size=gpu_size, num_workers=params.num_workers
    )

    """
    4. Initiate evaluation kit. 
    """
    eval_data = text_dataset["val"] + text_dataset["test"]
    val_state = [dt.state_name for dt in text_dataset["val"]]
    test_state = [dt.state_name for dt in text_dataset["test"]]
    eval_state = val_state + test_state
    eval_metric = [dt.metric for dt in eval_data]
    eval_funcs = [dt.meta_data["eval_func"] for dt in eval_data]
    loss = torch.nn.BCEWithLogitsLoss()
    evlter = []
    for dt in eval_data:
        if dt.metric == "acc":
            evlter.append(Accuracy(task="multiclass", num_classes=dt.classes))
        elif dt.metric == "auc":
            evlter.append(AUROC(task="binary"))
        elif dt.metric == "apr":
            evlter.append(MultiApr(num_labels=dt.classes))
        elif dt.metric == "aucmulti":
            evlter.append(MultiAuc(num_labels=dt.classes))
    metrics = EvalKit(
        eval_metric,
        evlter,
        loss,
        eval_funcs,
        flat_binary_func,
        eval_mode="max",
        exp_prefix="",
        eval_state=eval_state,
        val_monitor_state=val_state[0],
        test_monitor_state=test_state[0],
    )

    """
    5. Initiate optimizer, scheduler and lightning model module.
    """
    optimizer = torch.optim.Adam(
        model.parameters(), lr=params.lr, weight_decay=params.l2
    )
    lr_scheduler = {
        "scheduler": torch.optim.lr_scheduler.StepLR(optimizer, 15, 0.5),
        "interval": "epoch",
        "frequency": 1,
    }

    exp_config = ExpConfig(
        "",
        optimizer,
        dataset_callback=train_data.update,
        lr_scheduler=lr_scheduler,
    )
    exp_config.val_state_name = val_state
    exp_config.test_state_name = test_state

    pred_model = GraphPredLightning(exp_config, model, metrics)

    """
    6. Start training and logging.
    """



    strategy = "deepspeed_stage_2" if gpu_size > 1 else "auto"
    val_res, test_res = lightning_fit(
        None,
        pred_model,
        params.datamodule,
        metrics,
        params.num_epochs,
        strategy=strategy,
        save_model=False,
        load_best=params.load_best,
        reload_freq=1,
        test_rep=params.test_rep,
        val_interval=params.val_interval
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="rl")
    parser.add_argument("--override", type=str)

    parser.add_argument(
        "opts",
        default=[],
        nargs=argparse.REMAINDER,
        help="Modify config options using the command-line",
    )

    params = parser.parse_args()
    configs = []
    configs.append(
        load_yaml(
            os.path.join(
                os.path.dirname(__file__), "configs", "default_config.yaml"
            )
        )
    )

    if params.override is not None:
        override_config = load_yaml(params.override)
        configs.append(override_config)
    # Add for few-shot parameters

    mod_params = combine_dict(*configs)
    mod_params = merge_mod(mod_params, params.opts)
    setup_exp(mod_params)

    params = SimpleNamespace(**mod_params)
    set_random_seed(params.seed)

    torch.set_float32_matmul_precision("high")
    params.log_project = "full_cdm"

    params.exp_name += f"_{params.llm_name}_ofa1"

    print(params)
    main(params)