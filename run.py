import argparse
import yaml

import os
import random
import logging

import numpy as np
import pytorch_lightning as pl
import torch
from hydra.utils import instantiate
from pytorch_lightning.callbacks import ModelCheckpoint, LearningRateMonitor

from omegaconf import OmegaConf

from src.utils.export_model import ModelExport
from src.utils.tensorboard import TensorBoardLoggerWithMetrics
from src.utils.model_factory import ModelFactory
from src.utils.options import BaseOptions
from src.utils.versioning import get_git_diff

from hydra.experimental import compose, initialize
from sklearn.model_selection import ParameterGrid
from src.utils.checkpointing import set_latest_checkpoint

# register models
import src.models


def run(cfg: BaseOptions):
    # resolve variable interpolation
    cfg = OmegaConf.to_container(cfg, resolve=True)
    cfg = OmegaConf.create(cfg)
    OmegaConf.set_struct(cfg, True)

    logging.info("Configuration:\n%s" % OmegaConf.to_yaml(cfg))

    if not torch.cuda.is_available():
        logging.info("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        logging.info("!!! CUDA is NOT available !!!")
        logging.info("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

    # Create data module
    # Note: we need to manually process the data module in order to get a valid skeleton（我们需要手动处理数据模块，以获得有效的骨架）
    # We don't call setup() as this breaks ddp_spawn training for some reason
    # Calling setup() is not required here as we only need access to the skeleton data, achieved with get_skeleton()
    dm = instantiate(cfg.dataset)#返回base中dataset._target_指明的LafanSequenceDataset实例化对象
    dm.prepare_data()

    # create model
    model = ModelFactory.instantiate(cfg, skeleton=dm.get_skeleton())

    # setup logging
    metrics = model.get_metrics()
    tb_logger = TensorBoardLoggerWithMetrics(save_dir=cfg.logging.path,
                                             name=cfg.logging.name,
                                             version=cfg.logging.version,
                                             metrics=metrics)
    all_loggers = [tb_logger]

    # setup callbacks
    callbacks = []
    callbacks.append(ModelCheckpoint(dirpath=tb_logger.log_dir + '/checkpoints', save_last=cfg.logging.checkpoint.last,
                                     save_top_k=cfg.logging.checkpoint.top_k, monitor=cfg.logging.checkpoint.monitor,
                                     mode=cfg.logging.checkpoint.mode, every_n_epochs=cfg.logging.checkpoint.every_n_epochs))
    if cfg.logging.export_period > 0:
        callbacks.append(ModelExport(dirpath=tb_logger.log_dir + '/exports', filename=cfg.logging.export_name, period=cfg.logging.export_period))
    callbacks.append(LearningRateMonitor(logging_interval='step'))

    # Set random seem to guarantee proper working of distributed training
    # Note: we do it just before instantiating the trainer to guarantee nothing else will break it
    rnd_seed = cfg.seed
    random.seed(rnd_seed)
    np.random.seed(rnd_seed)
    torch.manual_seed(rnd_seed)
    logging.info("Random Seed: %i" % rnd_seed)

    # Save Git diff for reproducibility
    current_log_path = os.path.normpath(os.getcwd() + "/./" + tb_logger.log_dir)
    logging.info("Logging saved to: %s" % current_log_path)
    if not os.path.exists(current_log_path):
        os.makedirs(current_log_path)
    git_diff = get_git_diff()
    if git_diff != "":
        with open(current_log_path + "/git_diff.patch", 'w') as f:
            f.write(git_diff)

    # training
    trainer = pl.Trainer(logger=all_loggers, callbacks=callbacks, **cfg.trainer)
    trainer.fit(model, datamodule=dm)

    # export
    model.export(tb_logger.log_dir + '/model.onnx')

    # test
    metrics = model.evaluate()
    
    print("=================== LAFAN BENCHMARK ========================")
    print("L2Q@5", "L2Q@15", "L2Q@30", "L2P@5", "L2P@15", "L2P@30", "NPSS@5","NPSS@15", "NPSS@30", )
    print("{:.3f}".format(metrics["L2Q@5"]), "{:.4f}".format(metrics["L2Q@15"]), "{:.4f}".format(metrics["L2Q@30"]), \
          "{:.3f}".format(metrics["L2P@5"]), "{:.4f}".format(metrics["L2P@15"]), "{:.4f}".format(metrics["L2P@30"]), \
          "{:.4f}".format(metrics["NPSS@5"]), "{:.5f}".format(metrics["NPSS@15"]), "{:.5f}".format(metrics["NPSS@30"]))
    print("============================================================")
    

def main(filepath: str, overrides: list = []):
    with open(filepath) as f:
        experiment_cfg = yaml.load(f, Loader=yaml.SafeLoader)#加载transformer.yaml

    config_path = "src/configs"
    initialize(config_path=config_path)

    base_config = experiment_cfg["base_config"]#取出transformer.yaml中的base
    experiment_params = experiment_cfg["parameters"]#取出transformer.yaml中的params
    for k in experiment_params:
        if not isinstance(experiment_params[k], list):#确保所有params是list
            experiment_params[k] = [experiment_params[k]]

    param_grid = ParameterGrid(experiment_params)
    for param_set in param_grid:
        param_overrides = []

        for k in param_set:
            param_overrides.append(k + "=" + str(param_set[k]))

        # add global overrides last
        param_overrides += overrides

        cfg = compose(base_config + ".yaml", overrides=param_overrides)#配合initialize（）作全局初始化，base作为基础配置，base写在transformer.yaml里，transformer.yaml中其他配置被param_grid组织成附加的一组组配置override进cfg

        set_latest_checkpoint(cfg)

        run(cfg.model)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False, description="Experiment")
    parser.add_argument('--config', type=str, help='Path to the experiment configuration file', required=True)
    parser.add_argument("overrides", nargs="*",
                        help="Any key=value arguments to override config values (use dots for.nested=overrides)", )
    args = parser.parse_args()

    main(filepath=args.config, overrides=args.overrides)
