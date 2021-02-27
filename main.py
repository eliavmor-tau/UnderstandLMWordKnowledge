from data.tokenizer import Tokenizer
from models.mlm_models import *
from transformers import AutoConfig
import torch
from torch.nn.functional import softmax
from data.data_reader import DataReader
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import pickle
import json
from nltk.corpus import wordnet as wn
from data.wordnet_parser import WordNetObj
from data.concept_net import ConceptNetObj
from itertools import permutations, combinations
from collections import defaultdict
import time
import os

# model_name = 'bert-large-uncased'
model_name = 'roberta-large'
mc_mlm = True
config = AutoConfig.from_pretrained(model_name)
tokenizer = Tokenizer(model_name)
model = TransformerMaskedLanguageModel(vocab=config, model_name=model_name, multi_choice=mc_mlm)
data_reader = DataReader(host=DB_HOST, port=DB_PORT, password=DB_PASSWORD)
concept_net = ConceptNetObj()


def test_sentence_mc_mlm(sentence, mask_index, batch_size, target_index, multi_choice_answers, k=1):
    input_sent = tokenizer.encode(sentence, add_special_tokens=True) # TODO: move to config?

    mask_index += len(input_sent) - 2 - len(sentence.split(" "))
    if sentence.endswith("."):
        mask_index -= 1

    input_sent[mask_index] = tokenizer.mask_token_id()
    multi_choice_label_ids = tokenizer.convert_tokens_to_ids(multi_choice_answers)

    input_tensor = torch.tensor(input_sent).view((batch_size, -1))
    token_type_ids = torch.tensor([0] * len(input_sent)).view((batch_size, -1))

    # Mark legal labels.
    legal_labels = torch.tensor([0] * config.vocab_size)
    legal_labels[multi_choice_label_ids] = 1
    legal_labels = legal_labels.view((batch_size, -1))

    all_indices_mask = torch.zeros((batch_size, config.vocab_size))
    all_indices_mask[:, multi_choice_label_ids] = 1

    # Create a tensor of correct labels (only masked word != -100)
    labels = torch.tensor([-100] * len(input_sent))
    labels[mask_index] = multi_choice_label_ids[target_index]
    labels = labels.view((batch_size, -1))

    output = model(input_ids=input_tensor, token_type_ids=token_type_ids, all_indices_mask=all_indices_mask, labels=labels)

    output_softmax = softmax(output["logits"], dim=2)
    argmax_output = torch.topk(output_softmax, k=k, dim=2)[1]
    masked_predictions = argmax_output[0][mask_index]
    predictions = []
    for pred in masked_predictions:
        predictions.append((tokenizer.convert_ids_to_tokens(pred.item()).replace('Ġ', ''), output_softmax[0][mask_index][pred.item()].item()))
    return predictions


def test_sentence_mlm(sentence, mask_index, batch_size, k=1):
    input_sent = tokenizer.encode(sentence, add_special_tokens=True)  # TODO: move to config?
    mask_index += len(input_sent) - 2 - len(sentence.split(" "))
    if sentence.endswith("."):
        mask_index -= 1
    input_sent[mask_index] = tokenizer.mask_token_id()
    input_tensor = torch.tensor(input_sent).view((batch_size, -1))
    token_type_ids = torch.tensor([0] * len(input_sent)).view((batch_size, -1))

    output = model(input_ids=input_tensor, token_type_ids=token_type_ids)

    output_softmax = softmax(output["logits"], dim=2)
    argmax_output = torch.topk(output_softmax, k=k, dim=2)[1]
    masked_predictions = argmax_output[0][mask_index]
    predictions = []
    for pred in masked_predictions:
        predictions.append((tokenizer.convert_ids_to_tokens(pred.item()).replace('Ġ', ''),
                            output_softmax[0][mask_index][pred.item()].item()))
    return predictions


def filter_data_by_category(categories, k=20):
    base_sent = f"A <entity> is a type of {tokenizer.mask_token()}."
    mask_index = 7
    batch_size = 1

    for idx, category in enumerate(categories):
        target_index = idx  # TODO: move to config?
        data = data_reader.generate_is_a_sentences(category=categories[target_index], base_sent=base_sent,
                                                   entity_mask="<entity>", category_mask="<category>")
        true_scores = []
        false_scores = []
        p_list = []
        prediction_list = []
        top_k_options = []
        accuracy = 0
        for idx, row in data.iterrows():
            word = row["sentence"].split(" ")[1]
            sent = row["sentence"]
            orig_len = len(sent.split(" "))
            sent = sent.replace("_", " ")
            new_len = len(sent.split(" "))
            sent_mask_index = mask_index + (new_len - orig_len)
            output = test_sentence_mlm(sent, mask_index=sent_mask_index, batch_size=batch_size, k=k)
            predictions, p = [x[0] for x in output], [x[1] for x in output]
            top_k_options.append(predictions)

            if categories[target_index] in word:
                prediction = ""
                p = 0

            elif categories[target_index] in predictions:
                for i in range(len(predictions)):
                    if predictions[i] == categories[target_index]:
                        p = p[i]
                        break
                prediction = categories[target_index]

            else:
                prediction = predictions[0]
                p = p[0]

            prediction_list.append(prediction)
            p_list.append(p)
            status = prediction == categories[target_index]
            accuracy += 1 if status else 0
            if status:
                true_scores.append((word, p))
            else:
                false_scores.append((word, p))

        accuracy = accuracy / len(data)

        plt.title(f"{model_name} P[{categories[target_index]}| {base_sent}]\n Accuracy={accuracy}")
        plt.hist([s[1] for s in false_scores], color='red', label="False Prediction")
        plt.hist([s[1] for s in true_scores], color='blue', label="True Prediction", alpha=0.4)
        plt.legend()
        plt.savefig(f"graphs/{model_name}_{categories[target_index]}.graphs")
        plt.close()
        data.insert(loc=0, column="prediction_probability", value=p_list)
        data.insert(loc=0, column="prediction", value=prediction_list)
        data.insert(loc=0, column=f"top_{k}_predictions", value=top_k_options)
        data = data[data["prediction"] == categories[target_index]]
        data.to_csv(f"./csv/{model_name}_{categories[target_index]}.csv")


def group_entities_using_wordnet(csv_path):
    df = pd.read_csv(csv_path)
    groups = dict()
    count = 0
    for entity in df["name"]:
        try:
            hypernyms = WordNetObj.get_entity_hypernyms(entity)
            count += int(len(hypernyms) > 0)
            for hypernym in hypernyms:
                if hypernym not in groups:
                    groups[hypernym] = dict()
                    groups[hypernym]["entities"] = list()
                    groups[hypernym].update(concept_net.get_information_on_entity(hypernym, update_db=True))
                groups[hypernym]["entities"].append(entity)

        except Exception as e:
            print(f"Error in entity {entity}:", e)
            time.sleep(60)
    return groups


def plot_mc_overgeneralization(test_log, title, cmap="gray", output_path=""):
    rows = list([key for key in test_log.keys() if key != "wrong_answers"])
    cols = test_log["wrong_answers"]
    row_to_idx = {entity: idx for idx, entity in enumerate(rows)}
    col_to_idx = {entity: idx + 1 for idx, entity in enumerate(cols)}

    heatmap = np.zeros((len(rows), len(cols) + 2))

    for e1 in rows:
        for e2, hits in test_log[e1].items():
            if e1 == e2:
                heatmap[row_to_idx[e1], 0] = hits
            else:
                heatmap[row_to_idx[e1], col_to_idx[e2] if e2 in col_to_idx else -1] = hits
    y_labels = [x[0] for x in sorted([(e, idx)for e, idx in row_to_idx.items()], key=lambda y: y[1])]
    x_labels = ["correct answer"] + [x[0] for x in sorted([(e, idx)for e, idx in col_to_idx.items()], key=lambda y: y[1])] + ["other"]
    plt.matshow(heatmap, cmap=cmap)
    plt.yticks(np.arange(heatmap.shape[0]), y_labels, fontsize=6)
    plt.xticks(np.arange(heatmap.shape[1]), x_labels, rotation="vertical", fontsize=6)
    plt.xlabel("All Answers")
    plt.ylabel("Correct Answer")
    if heatmap.shape[0] < 25 and heatmap.shape[1] < 25:
        for y in range(heatmap.shape[0]):
            for x in range(heatmap.shape[1]):
                plt.annotate("{}".format(int(heatmap[y, x])), (x - 0.25, y), color="white", fontsize=5)
    plt.colorbar()
    plt.title(title)
    if output_path:
        plt.savefig(output_path, bbox_inches='tight')
    else:
        plt.show()
    plt.close()


def plot_overgeneralization(test_log, output_path=""):
    rows, cols = list(test_log.keys()), ["correct", "overgeneralization", "other"]
    figure, ax = plt.subplots(figsize=(10, 6))
    cell_text = []
    for row in rows:
        cell_text.append(["{:.3f}".format(x) for x in test_log[row]])

    # Add a table at the bottom of the axes
    plt.table(cellText=cell_text,
                          rowLabels=rows,
                          colLabels=cols,
                          loc="center")
    figure.patch.set_visible(False)
    ax.axis('off')
    ax.axis('tight')
    figure.tight_layout()

    if output_path:
        plt.savefig(output_path)
    else:
        plt.show()
    plt.close()


def over_generalization_metric(sentence, mask_index, k, generalization, over_generalization, debug=False):
    res = test_sentence_mlm(sentence=sentence, mask_index=mask_index, batch_size=1, k=k)
    correct_score = 0
    over_generalization_score = 0
    mistake_score = 0
    for entity, p in res:
        synset = wn.synsets(entity)
        if synset:
            hypernyms = [entity]
            for s in synset:
                hyper = lambda x: x.hypernyms()
                hypernyms += [x.name().partition('.')[0] for x in s.closure(hyper)]

            hypernyms = set(hypernyms)
            if generalization.intersection(hypernyms):
                correct_score += p
                if debug:
                    print(f"generalization: {entity} p={p}", hypernyms)
            elif over_generalization.intersection(hypernyms):
                over_generalization_score += p
                if debug:
                    print(f"over-generalization: {entity} p={p}", hypernyms)
            else:
                mistake_score += p
                if debug:
                    print(f"mistake: {entity} p={p}", hypernyms)

    normalization_factor = mistake_score + correct_score + over_generalization_score
    correct_score /= normalization_factor
    over_generalization_score /= normalization_factor
    mistake_score /= normalization_factor
    if debug:
        print("*" * 100)
        print(f"tested sentence: {sentence}")
        print("correct score:", correct_score)
        print("over generalization score:", over_generalization_score)
        print("other score:", mistake_score)
    return [correct_score, over_generalization_score, mistake_score]


def filter_word_in_model_vocab(tokenizer, words):
    known_words = [w for w in words if tokenizer.convert_tokens_to_ids(w) != tokenizer.unk_id()]
    return known_words


def filter_word_not_in_model_vocab(tokenizer, words):
    known_words = [w for w in words if tokenizer.convert_tokens_to_ids(w) == tokenizer.unk_id()]
    return known_words


def mc_over_generalization_test(base_sent, mask_index, correct_classes, incorrect_classes, tokenizer, data, output_name,
                                comb_size=1):
    correct_answers = correct_classes
    wrong_answers = incorrect_classes
    for x in correct_classes:
        if x in data:
            correct_answers = correct_answers.union(set(data[x]["entities"]))
    for x in incorrect_classes:
        if x in data:
            wrong_answers = wrong_answers.union(set(data[x]["entities"]))

    # Filter words in model's vocabulary
    correct_answers = filter_word_in_model_vocab(tokenizer=tokenizer, words=correct_answers)
    wrong_answers = filter_word_in_model_vocab(tokenizer=tokenizer, words=wrong_answers)

    wrong_answers_comb = [list(x) for x in
                             combinations(wrong_answers, len(wrong_answers) if comb_size < 0 else comb_size)]
    # Log test
    log = dict()
    log["wrong_answers"] = list(wrong_answers)
    for correct_ans in correct_answers:
        log[correct_ans] = defaultdict(float)
        for wrong_ans in wrong_answers_comb:
            multi_choice_answers = [correct_ans] + wrong_ans
            prediction, p = \
            test_sentence_mc_mlm(base_sent, multi_choice_answers=multi_choice_answers, target_index=0,
                                 mask_index=mask_index, batch_size=1)[0]
            log[correct_ans][prediction] += 1

    pickle_base_path = os.path.join(os.path.join("pickle", f"1_vs_{'all' if comb_size == -1 else comb_size}"))
    graphs_base_path = os.path.join(os.path.join("graphs", f"1_vs_{'all' if comb_size == -1 else comb_size}"))
    os.makedirs(pickle_base_path, exist_ok=True)
    os.makedirs(graphs_base_path, exist_ok=True)
    graph_path = os.path.join(graphs_base_path, output_name + ".jpg")
    pickle_path = os.path.join(pickle_base_path, output_name + ".pkl")
    with open(pickle_path, "wb") as f:
        pickle.dump(log, f)

    plot_mc_overgeneralization(test_log=log, title=base_sent, cmap="RdBu", output_path=graph_path)


def preprocess_data():
    filter_data_by_category(["animal"])
    groups = group_entities_using_wordnet(f"./csv/{model_name}_animal.csv")
    with open(f"pickle/{model_name}_animal_groups.pkl", "wb") as f:
        pickle.dump(groups, f)

    with open(f"json/{model_name}_animal_groups.json", "w") as f:
        json.dump(groups, f)

def load_data():
    with open(f"pickle/{model_name}_animal_groups.pkl", "rb") as f:
        data = pickle.load(f)
    return data


def run_mc_overgeneralization_metric(tests_path="config/overgenerazliation_tests.json", test_name=""):
    with open(tests_path, "r") as f:
        tests = json.load(f)

    data = load_data()
    for tn, test_data in tests.items():
        run_test = not len(test_name) or test_name == tn
        mask_index = test_data["mask_index"]
        sentences = test_data["sentences"]
        correct_classes = set(test_data["correct_classes"])
        incorrect_classes = set(test_data["mc_overgeneralize_classes"])
        if run_test:
            for sent in sentences:
                for comb_size in test_data["comb_size"]:
                    sent = sent.split(" ")
                    sent[mask_index - 1] = tokenizer.mask_token()
                    sent = " ".join(sent)
                    output_name = f"{model_name}_{sent.replace(' ', '_').replace('.', '')}"
                    mc_over_generalization_test(base_sent=sent, mask_index=mask_index,
                                                correct_classes=correct_classes,
                                                incorrect_classes=incorrect_classes,
                                                tokenizer=tokenizer,
                                                data=data,
                                                output_name=output_name, comb_size=comb_size)


def run_overgeneralization_metric(tests_path="config/overgenerazliation_tests.json", test_name="", K=1000, debug=False):
    with open(tests_path, "r") as f:
        tests = json.load(f)

    test_log = {}
    for tn, test_data in tests.items():
        run_test = not len(test_name) or test_name == tn
        mask_index = test_data["mask_index"]
        sentences = test_data["sentences"]
        correct_classes = set(test_data["correct_classes"])
        incorrect_classes = set(test_data["overgeneralize_classes"])
        if run_test:
            for sent in sentences:
                sent = sent.split(" ")
                sent[mask_index - 1] = tokenizer.mask_token()
                sent = " ".join(sent)
                scores = over_generalization_metric(sent, mask_index, K, correct_classes, incorrect_classes, debug=debug)
                test_log[sent] = scores

    output_path = os.path.join("graphs", "overgeneralization_metric")
    os.makedirs(output_path, exist_ok=True)
    plot_overgeneralization(test_log, output_path=os.path.join(output_path, f"{model_name}_overgeneralization_metric.jpg"))


if __name__ == "__main__":
    run_mc_overgeneralization_metric()
    # run_overgeneralization_metric(K=tokenizer.get_vocab_len(), debug=True)
