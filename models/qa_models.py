import pytorch_lightning as pl
from pytorch_lightning import Trainer
from transformers import T5Tokenizer, T5ForConditionalGeneration, get_linear_schedule_with_warmup
from torch.utils.data import DataLoader, Dataset, RandomSampler
from torch.optim import AdamW, Adam
import pandas as pd
import torch
import numpy as np
import os
import logging
import pickle
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from torch.optim.optimizer import Optimizer

logger = logging.getLogger(__name__)


class YesNoDataSet(Dataset):

    def __init__(self, csv_path, tokenizer, max_length):
        self.tokenizer = tokenizer
        self.df = pd.read_csv(csv_path)
        self.max_length = max_length
        self.yes_questions = self.df[self.df.label == 'Yes']
        self.no_questions = self.df[self.df.label == 'No']
        self.questions = self.df.question.values
        self.labels = self.df.label.values

    def __len__(self):
        return len(self.questions)

    def pad(self, sample):
        sample_len = len(sample)
        padding_len = self.max_length - sample_len
        pad_sample = torch.hstack([torch.LongTensor(sample), torch.LongTensor([self.tokenizer.pad_token_id] * padding_len)])
        return pad_sample

    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        question = self.questions[idx]
        encoded_question = self.tokenizer.encode_plus(question, return_tensors="pt", max_length=self.max_length,
                                                      padding='max_length')
        encoded_label = self.tokenizer.encode_plus(self.labels[idx] + " </s>", max_length=self.max_length, padding='max_length',
                                                   return_tensors="pt")
        input_ids = encoded_question.input_ids.squeeze()
        attention_mask = encoded_question.attention_mask.squeeze()
        label = encoded_label.input_ids.squeeze()

        input_ids = self.pad(input_ids)
        attention_mask = self.pad(attention_mask)
        sample = {'input_ids': input_ids, 'attention_mask': attention_mask, 'labels': label}
        return sample


class YesNoQuestionAnswering(pl.LightningModule):
    def __init__(self, model, tokenizer, config):
        super().__init__()
        self.model = model
        self.tokenizer = tokenizer
        self.config = config

    def forward(self, input_ids=None, attention_mask=None, labels=None):
        output = self.model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
        return output

    def _step(self, batch):
        input_ids = batch["input_ids"]
        attention_mask = batch["attention_mask"]
        labels = batch["labels"]
        labels[labels[:, :] == self.tokenizer.pad_token_id] = -100
        output = self(input_ids, attention_mask, labels)
        loss = output.loss
        return loss

    def training_step(self, batch, batch_idx):
        loss = self._step(batch)
        tensorboard_logs = {"train_loss": loss}
        self.log("train_loss", loss)
        return {"loss": loss, "log": tensorboard_logs}

    def training_epoch_end(self, outputs):
        avg_train_loss = torch.stack([x["loss"] for x in outputs]).mean()
        tensorboard_logs = {"avg_train_loss": avg_train_loss}
        return {"avg_train_loss": avg_train_loss, "log": tensorboard_logs, 'progress_bar': tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        loss = self._step(batch)
        return {"val_loss": loss}

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        tensorboard_logs = {"val_loss": avg_loss}
        return {"avg_val_loss": avg_loss, "log": tensorboard_logs, 'progress_bar': tensorboard_logs}

    def configure_optimizers(self):
        "Prepare optimizer and schedule (linear warmup and decay)"
        model = self.model
        optimizer = Adam(model.parameters(), lr=self.config['lr'])
        self.opt = optimizer
        return [optimizer]

    def get_tqdm_dict(self):
        tqdm_dict = {"loss": "{:.3f}".format(self.trainer.avg_loss), "lr": self.lr_scheduler.get_last_lr()[-1]}
        return tqdm_dict

    def train_dataloader(self):
        dataset = YesNoDataSet(csv_path=self.config.get("train_data"), tokenizer=self.tokenizer, max_length=self.config["max_length"])
        dataloader = DataLoader(dataset, batch_size=self.config.get("batch_size"), shuffle=True, num_workers=4)
        return dataloader

    def val_dataloader(self):
        dataset = YesNoDataSet(csv_path=self.config.get("dev_data"), tokenizer=self.tokenizer, max_length=self.config["max_length"])
        dataloader = DataLoader(dataset, batch_size=self.config.get("batch_size"), shuffle=False, num_workers=4)
        return dataloader


def train_model(config):
    checkpoint_callback = pl.callbacks.ModelCheckpoint(dirpath="checkpoint", prefix="checkpoint", monitor="val_loss",
                                                       mode="min", save_top_k=5)
    train_params = dict(
        gpus=config.get("gpus"),
        max_epochs=config.get("max_epochs", 1),
        checkpoint_callback=checkpoint_callback
    )
    logging.info(config)
    tokenizer = T5Tokenizer.from_pretrained(config.get("model_name"), cache_dir="../cache/")
    model = T5ForConditionalGeneration.from_pretrained(config.get("model_name"), cache_dir="../cache/")
    model = YesNoQuestionAnswering(tokenizer=tokenizer, model=model, config=config)
    if config.get("checkpoint", None):
        logging.info("Use checkpoint")
        checkpoint = torch.load(config.get("checkpoint"), map_location=torch.device(config.get("device")))
        model.load_state_dict(checkpoint["state_dict"])

    trainer = Trainer(**train_params)
    trainer.fit(model)
    with open("pickle/training_loss.pkl", "wb") as f:
        pickle.dump(model.model_training_loss, f)

    with open("pickle/validation_loss.pkl", "wb") as f:
        pickle.dump(model.model_validation_loss, f)


def test_model(config, output_path):
    print("Test Model")
    print(config.get("test_data"))
    tokenizer = T5Tokenizer.from_pretrained(config.get("model_name"), cache_dir="../cache/")
    model = T5ForConditionalGeneration.from_pretrained(config.get("model_name"), cache_dir="../cache/")
    model = YesNoQuestionAnswering(tokenizer=tokenizer, model=model, config=config)

    if config.get("checkpoint", None):
        checkpoint = torch.load(config.get("checkpoint"), map_location=torch.device(config.get("device")))
        model.load_state_dict(checkpoint["state_dict"])
    print("Load checkpoint")
    model.eval()
    data_set = YesNoDataSet(csv_path=config.get("test_data", "csv/test_questions.csv"), tokenizer=tokenizer, max_length=config["max_length"])
    data_loader = DataLoader(data_set, batch_size=config.get("batch_size"), shuffle=True)

    accuracy = 0.0
    count = 0.0
    questions = []
    model_answers = []
    true_answers = []
    for batch in data_loader:
        input_ids = batch["input_ids"]
        attention_mask = batch["attention_mask"]
        labels = batch["labels"]
        output = model.model.generate(input_ids=input_ids,
                                              attention_mask=attention_mask,
                                              max_length=2)
        for idx, answer in enumerate(output):
            model_answer = tokenizer.decode(answer, skip_special_tokens=True, clean_up_tokenization_spaces=True)
            true_answer = tokenizer.decode(labels[idx], skip_special_tokens=True, clean_up_tokenization_spaces=True)
            question = tokenizer.decode(input_ids[idx], skip_special_tokens=True, clean_up_tokenization_spaces=True)
            model_answers.append(model_answer)
            true_answers.append(true_answer)
            questions.append(question)
            print("Question:", tokenizer.decode(input_ids[idx], skip_special_tokens=True, clean_up_tokenization_spaces=True))
            print("Model answer:", model_answer)
            print("True answer:", true_answer)
            print("-" * 20)
            if true_answer == model_answer:
                accuracy += 1
            count += 1
    result_df = pd.DataFrame.from_dict({"question": questions, "model_answer": model_answers, "true_answer": true_answers})
    result_df.to_csv(output_path)
    print("Accuracy:", accuracy / count)


if __name__ == "__main__":

    torch.cuda.empty_cache()
    config = {
        "train": False,
        "model_name": "t5-base",
        "gpus": 1,
        "max_epochs": 30,
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "batch_size": 8,
        "train_data": "csv/train_no_animals_and_fruits_questions.csv",
        "test_data": "csv/animals_dont_live_underwater_questions.csv",
        "dev_data": "csv/val_no_animals_and_fruits_questions.csv",
        "lr": 1e-4,
        #"checkpoint": None,
        "checkpoint": "checkpoint/checkpoint-epoch=0-step=10322.ckpt",
        "gradient_clip_val": 1.0,
        "gradient_accumulation_steps" : 16,
        "max_length": 128,
        "weight_decay": 0.0,
        "adam_epsilon": 1e-8,
        "warmup_steps": 0,
    }

    print("Start Run")
    print("- Config -")
    for k, v in config.items():
        print(f"{k}: {v}")
    
    if config.get("train", True):
        train_model(config)
    else:
        test_files = ["csv/sanity", "csv/animals_have_a_beak", "csv/animals_have_horns", "csv/animals_have_fins", "csv/animals_have_scales",
               "csv/animals_have_wings", "csv/animals_have_feathers", "csv/animals_have_fur",
               "csv/animals_have_hair", "csv/animals_live_underwater", "csv/animals_can_fly",
               "csv/animals_dont_have_a_beak", "csv/animals_dont_have_horns", "csv/animals_dont_have_fins",
               "csv/animals_dont_have_scales", "csv/animals_dont_have_wings", "csv/animals_dont_have_feathers",
               "csv/animals_dont_have_fur", "csv/animals_dont_have_hair", "csv/animals_dont_live_underwater",
               "csv/animals_cant_fly"]
        test_files = ["csv/animals_can_fly", "csv/animals_cant_fly"]
        for f in test_files:
            config["test_data"] = f"{f}_questions.csv"
           # config["test_data"] = f"{f}"
            test_model(config, config["test_data"].replace(".csv", "_result.csv").replace("csv/", "csv/results/"))
