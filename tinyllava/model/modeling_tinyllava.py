from dataclasses import dataclass
from typing import List, Optional, Tuple, Union
import ast

import torch
import torch.utils.checkpoint
from torch import nn

from transformers import PreTrainedModel
from transformers.modeling_outputs import CausalLMOutputWithPast
from transformers.generation.utils import GenerateOutput

from . import LLMFactory, ConnectorFactory, VisionTowerFactory
from .configuration_tinyllava import TinyLlavaConfig
from ..utils.constants import *
# from tinyllava.utils.data_utils import get_value_from_kwargs

#from safetensors import safe_open
from safetensors.torch import load_file
import os
import json

def get_value_from_kwargs(kwargs, name):
    if name in kwargs:
        return kwargs.pop(name)
    else:
        return None
    


class TinyLlavaPreTrainedModel(PreTrainedModel):
    config_class = TinyLlavaConfig
    base_model_prefix = "model"
    supports_gradient_checkpointing = True
    _no_split_modules = ["LlavaVisionAttention"]
    _skip_keys_device_placement = "past_key_values"
    _supports_flash_attn_2 = True

    def _init_weights(self, module):
        std = (
            self.config.initializer_range
            if hasattr(self.config, "initializer_range")
            else self.config.text_config.initializer_range
        )

        if hasattr(module, "class_embedding"):
            module.class_embedding.data.normal_(mean=0.0, std=std)

        if isinstance(module, (nn.Linear, nn.Conv2d)):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    @property
    def _supports_sdpa(self):
        return self.language_model._supports_sdpa


class TinyLlavaForConditionalGeneration(TinyLlavaPreTrainedModel):
    def __init__(self, config: TinyLlavaConfig):
        
        super().__init__(config)

        self.language_model = LLMFactory(config.llm_model_name_or_path)[0](config.text_config)
        self.vision_tower = VisionTowerFactory(config.vision_model_name_or_path)(config.vision_config)
        self.connector = ConnectorFactory(config.connector_type)(config)
        
        (Tokenizer, post_load) = LLMFactory(config.llm_model_name_or_path)[1]
        self.tokenizer = post_load(Tokenizer.from_pretrained(
            config.tokenizer_name_or_path,
            cache_dir = config.cache_dir,
            model_max_length = config.tokenizer_model_max_length,
            padding_side = config.tokenizer_padding_side,
            use_fast = config.tokenizer_use_fast,
        ))
        self.post_init()

    
    def get_input_embeddings(self):
        return self.language_model.get_input_embeddings()

    def set_input_embeddings(self, value):
        self.language_model.set_input_embeddings(value)

    def get_output_embeddings(self):
        return self.language_model.get_output_embeddings()

    def set_output_embeddings(self, new_embeddings):
        self.language_model.set_output_embeddings(new_embeddings)

    def set_decoder(self, decoder):
        self.language_model.set_decoder(decoder)

    def get_decoder(self):
        return self.language_model.get_decoder()

    def tie_weights(self):
        return self.language_model.tie_weights()

    def resize_token_embeddings(self, new_num_tokens: Optional[int] = None, pad_to_multiple_of=None) -> nn.Embedding:
        model_embeds = self.language_model.resize_token_embeddings(new_num_tokens, pad_to_multiple_of)
        # update vocab size
        self.config.text_config.vocab_size = model_embeds.num_embeddings
        self.config.vocab_size = model_embeds.num_embeddings
        self.vocab_size = model_embeds.num_embeddings
        return model_embeds

    
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        images: Optional[torch.FloatTensor] = None,
        image_sizes: Optional[List[List[int]]] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, CausalLMOutputWithPast]:
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        if inputs_embeds is None:
            (
                input_ids,
                position_ids,
                attention_mask,
                past_key_values,
                inputs_embeds,
                labels
            ) = self.prepare_inputs_labels_for_multimodal(
                input_ids,
                position_ids,
                attention_mask,
                past_key_values,
                labels,
                images,
                image_sizes
            )
        return self.language_model.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            labels=labels,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict
        )
    
    @torch.no_grad()
    def generate(
        self,
        inputs: Optional[torch.Tensor] = None,
        images: Optional[torch.Tensor] = None,
        image_sizes: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> Union[GenerateOutput, torch.LongTensor]:
        position_ids = kwargs.pop("position_ids", None)
        attention_mask = kwargs.pop("attention_mask", None)
        if "inputs_embeds" in kwargs:
            raise NotImplementedError("`inputs_embeds` is not supported")

        if images is not None:
            (
                inputs,
                position_ids,
                attention_mask,
                _,
                inputs_embeds,
                _
            ) = self.prepare_inputs_labels_for_multimodal(
                inputs,
                position_ids,
                attention_mask,
                None,
                None,
                images,
                image_sizes=image_sizes
            )
        else:
            inputs_embeds = self.language_model.get_input_embeddings()(inputs)

        return self.language_model.generate(
            position_ids=position_ids,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            **kwargs
        )
        
    def encode_images(self, images):
        kwargs = {}
        kwargs['vision_feature_layer'] = self.config.vision_feature_layer
        kwargs['vision_feature_select_strategy'] = self.config.vision_feature_select_strategy
        images = images.to(device=self.device, dtype=self.dtype)
        image_features = self.vision_tower(images, **kwargs)
        image_features = self.connector(image_features)
        return image_features
    
    
    
    def prepare_inputs_for_generation(self, input_ids, past_key_values=None,
                                      inputs_embeds=None, **kwargs):
        images = kwargs.pop("images", None)
        image_sizes = kwargs.pop("image_sizes", None)
        inputs = self.language_model.prepare_inputs_for_generation(
            input_ids, past_key_values=past_key_values, inputs_embeds=inputs_embeds, **kwargs
        )
        if images is not None:
            inputs['images'] = images
        if image_sizes is not None:
            inputs['image_sizes'] = image_sizes
        return inputs
        
    def prepare_inputs_labels_for_multimodal(
        self, input_ids, position_ids, attention_mask, past_key_values, labels,
        images, image_sizes=None
    ):
        vision_tower = self.vision_tower
        if vision_tower is None or images is None or input_ids.shape[1] == 1:
            return input_ids, position_ids, attention_mask, past_key_values, None, labels

        
        image_features = self.encode_images(images)

        # TODO: image start / end is not implemented here to support pretraining.
        if getattr(self.config, 'tune_mm_mlp_adapter', False):
            raise NotImplementedError

        # Let's just add dummy tensors if they do not exist,
        # it is a headache to deal with None all the time.
        # But it is not ideal, and if you have a better idea,
        # please open an issue / submit a PR, thanks.
        _labels = labels
        _position_ids = position_ids
        _attention_mask = attention_mask
        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids, dtype=torch.bool)
        else:
            attention_mask = attention_mask.bool()
        if position_ids is None:
            position_ids = torch.arange(0, input_ids.shape[1], dtype=torch.long, device=input_ids.device)
        if labels is None:
            labels = torch.full_like(input_ids, IGNORE_INDEX)

        # remove the padding using attention_mask -- FIXME
        _input_ids = input_ids
        input_ids = [cur_input_ids[cur_attention_mask] for cur_input_ids, cur_attention_mask in zip(input_ids, attention_mask)]
        labels = [cur_labels[cur_attention_mask] for cur_labels, cur_attention_mask in zip(labels, attention_mask)]

        new_input_embeds = []
        new_labels = []
        cur_image_idx = 0
        for batch_idx, cur_input_ids in enumerate(input_ids):
            num_images = (cur_input_ids == IMAGE_TOKEN_INDEX).sum()
            if num_images == 0:
                cur_image_features = image_features[cur_image_idx]
                cur_input_embeds_1 = self.language_model.get_input_embeddings()(cur_input_ids)
                cur_input_embeds = torch.cat([cur_input_embeds_1, cur_image_features[0:0]], dim=0)
                new_input_embeds.append(cur_input_embeds)
                new_labels.append(labels[batch_idx])
                cur_image_idx += 1
                continue

            image_token_indices = [-1] + torch.where(cur_input_ids == IMAGE_TOKEN_INDEX)[0].tolist() + [cur_input_ids.shape[0]]
            cur_input_ids_noim = []
            cur_labels = labels[batch_idx]
            cur_labels_noim = []
            for i in range(len(image_token_indices) - 1):
                cur_input_ids_noim.append(cur_input_ids[image_token_indices[i]+1:image_token_indices[i+1]])
                cur_labels_noim.append(cur_labels[image_token_indices[i]+1:image_token_indices[i+1]])
            split_sizes = [x.shape[0] for x in cur_labels_noim]
            cur_input_embeds = self.language_model.get_input_embeddings()(torch.cat(cur_input_ids_noim))
            cur_input_embeds_no_im = torch.split(cur_input_embeds, split_sizes, dim=0)
            cur_new_input_embeds = []
            cur_new_labels = []

            for i in range(num_images + 1):
                cur_new_input_embeds.append(cur_input_embeds_no_im[i])
                cur_new_labels.append(cur_labels_noim[i])
                if i < num_images:
                    cur_image_features = image_features[cur_image_idx]
                    cur_image_idx += 1
                    cur_new_input_embeds.append(cur_image_features)
                    cur_new_labels.append(torch.full((cur_image_features.shape[0],), IGNORE_INDEX, device=cur_labels.device, dtype=cur_labels.dtype))

            cur_new_input_embeds = [x.to(self.device) for x in cur_new_input_embeds]

            cur_new_input_embeds = torch.cat(cur_new_input_embeds)
            cur_new_labels = torch.cat(cur_new_labels)

            new_input_embeds.append(cur_new_input_embeds)
            new_labels.append(cur_new_labels)

        # Truncate sequences to max length as image embeddings can make the sequence longer
        tokenizer_model_max_length = getattr(self.config, 'tokenizer_model_max_length', None)
        if tokenizer_model_max_length is not None:
            new_input_embeds = [x[:tokenizer_model_max_length] for x in new_input_embeds]
            new_labels = [x[:tokenizer_model_max_length] for x in new_labels]

        # Combine them
        max_len = max(x.shape[0] for x in new_input_embeds)
        batch_size = len(new_input_embeds)

        new_input_embeds_padded = []
        new_labels_padded = torch.full((batch_size, max_len), IGNORE_INDEX, dtype=new_labels[0].dtype, device=new_labels[0].device)
        attention_mask = torch.zeros((batch_size, max_len), dtype=attention_mask.dtype, device=attention_mask.device)
        position_ids = torch.zeros((batch_size, max_len), dtype=position_ids.dtype, device=position_ids.device)

        for i, (cur_new_embed, cur_new_labels) in enumerate(zip(new_input_embeds, new_labels)):
            cur_len = cur_new_embed.shape[0]
            if getattr(self.config, 'tokenizer_padding_side', 'right') == "left":
                new_input_embeds_padded.append(torch.cat((
                    torch.zeros((max_len - cur_len, cur_new_embed.shape[1]), dtype=cur_new_embed.dtype, device=cur_new_embed.device),
                    cur_new_embed
                ), dim=0))
                if cur_len > 0:
                    new_labels_padded[i, -cur_len:] = cur_new_labels
                    attention_mask[i, -cur_len:] = True
                    position_ids[i, -cur_len:] = torch.arange(0, cur_len, dtype=position_ids.dtype, device=position_ids.device)
            else:
                new_input_embeds_padded.append(torch.cat((
                    cur_new_embed,
                    torch.zeros((max_len - cur_len, cur_new_embed.shape[1]), dtype=cur_new_embed.dtype, device=cur_new_embed.device)
                ), dim=0))
                if cur_len > 0:
                    new_labels_padded[i, :cur_len] = cur_new_labels
                    attention_mask[i, :cur_len] = True
                    position_ids[i, :cur_len] = torch.arange(0, cur_len, dtype=position_ids.dtype, device=position_ids.device)

        new_input_embeds = torch.stack(new_input_embeds_padded, dim=0)

        if _labels is None:
            new_labels = None
        else:
            new_labels = new_labels_padded

        if _attention_mask is None:
            attention_mask = None
        else:
            attention_mask = attention_mask.to(dtype=_attention_mask.dtype)

        if _position_ids is None:
            position_ids = None
        return None, position_ids, attention_mask, past_key_values, new_input_embeds, new_labels
      
    
    def load_llm(self, **kwargs):
        full_state_dict = get_value_from_kwargs(kwargs, 'full_state_dict')
        if full_state_dict is not None:
            print('Loading language model weights from provided checkpoint (full_state_dict)...')
            llm_state_dict = {}
            for k, v in full_state_dict.items():
                if k.startswith('language_model.'):
                    llm_state_dict[k[15:]] = v
            
            try:
                self.language_model.load_state_dict(llm_state_dict, strict=False)
                print("Language model weights loaded successfully from full_state_dict.")
            except RuntimeError as e:
                print(f"Error loading language model weights from full_state_dict: {e}")
                # You might want to log this or raise a more specific error
                raise e # Or handle gracefully, e.g., fall back to original loading if possible
        else:
            # Original LLM loading logic (e.g., from huggingface/local path)
            language_model_name = get_value_from_kwargs(kwargs, 'model_name_or_path')
            pretrained_llm_path = get_value_from_kwargs(kwargs, 'pretrained_llm_path')
            if pretrained_llm_path is not None:
                language_model_name = pretrained_llm_path
            
            if language_model_name is not None:
                self.language_model = self.language_model.from_pretrained(
                    language_model_name, **kwargs
                )
            print('loading language model from ', language_model_name)
            
        self.language_model.requires_grad_(False) # Freeze LLM weights for finetuning

        self.config.text_config.torch_dtype = kwargs.get('torch_dtype', None)
        self.config.pad_token = getattr(self.tokenizer, 'pad_token', None)
        self.config.pad_token_id = getattr(self.tokenizer, 'pad_token_id', None)
        #self.config.tokenizer_padding_side = getattr(self.tokenizer, 'padding_side', None)
        #self.config.tokenizer_model_max_length =  getattr(self.tokenizer, 'model_max_length', None)
        
        
    def load_vision_tower(self, **kwargs):
        full_state_dict = get_value_from_kwargs(kwargs, 'full_state_dict')
        
        if full_state_dict is not None:
            print('Loading vision tower weights from provided checkpoint (full_state_dict)...')
            vision_tower_state_dict = {}
            for k, v in full_state_dict.items():
                if k.startswith('vision_tower.'):
                    vision_tower_state_dict[k[13:]] = v
            
            try:
                self.vision_tower.load_state_dict(vision_tower_state_dict, strict=True)
                print("Vision tower weights loaded successfully from full_state_dict.")
            except RuntimeError as e:
                print(f"Error loading vision tower weights from full_state_dict: {e}")
                raise e
        else:
            # Original Vision Tower loading logic
            vision_tower_name = get_value_from_kwargs(kwargs, 'model_name_or_path')
            self.vision_tower.load_model(vision_tower_name, **kwargs)
            print('loading vision tower from ', vision_tower_name)

        
    def load_connector(self, **kwargs):
        full_state_dict = get_value_from_kwargs(kwargs, 'full_state_dict')
        if full_state_dict is not None:
            print('Loading connector weights from provided checkpoint (full_state_dict)...')
            connector_state_dict = {}
            for k, v in full_state_dict.items():
                if k.startswith('connector.'):
                    connector_state_dict[k[10:]] = v
            
            try:
                self.connector.load_state_dict(connector_state_dict, strict=True)
                print("Connector weights loaded successfully from full_state_dict.")
            except RuntimeError as e:
                print(f"Error loading connector weights with strict=True from full_state_dict: {e}")
                print("Attempting to load with strict=False.")
                self.connector.load_state_dict(connector_state_dict, strict=False)
                print("Connector weights loaded successfully with strict=False from full_state_dict.")
        else:
            # Original Connector loading logic
            self.connector.load_model(**kwargs)
            print('loading connector with existing logic')
            
            
    @classmethod
    def from_pretrained(cls, model_config: TinyLlavaConfig, **kwargs):
        """
        TinyLlavaForConditionalGeneration 모델을 사전 학습된 체크포인트에서 로드합니다.
        config 객체를 직접 매개변수로 받으며, 'model.safetensors' 파일이 존재하는 경우 이를 우선적으로 사용합니다.
        """
        # config 객체를 이미 받았으므로, 여기서 다시 from_pretrained를 호출할 필요가 없습니다.
        # model_config는 TinyLlavaConfig 인스턴스여야 합니다.
        config = model_config 
        model = cls(config) # __init__을 통해 LLM, Vision Tower, Connector 초기화
        
        pretrained_path = get_value_from_kwargs(kwargs, 'pretrained_model_path')
        
        if os.path.isfile(pretrained_path) and pretrained_path.endswith('.safetensors'):
            print(f"Found safetensors file at {pretrained_path}. Loading checkpoint.")
            full_state_dict = load_file(pretrained_path)

        # Case 2: 디렉토리인 경우
        elif os.path.isdir(pretrained_path):
            # model.safetensors가 있는지 확인
            single_file = os.path.join(pretrained_path, "model.safetensors")
            index_file = os.path.join(pretrained_path, "model.safetensors.index.json")

            if os.path.isfile(single_file):
                print(f"Found model.safetensors at {single_file}. Loading checkpoint.")
                full_state_dict = load_file(single_file)

            elif os.path.isfile(index_file):
                print(f"Found safetensors index at {index_file}. Loading split checkpoint from directory.")
                    
                full_state_dict = {}
                with open(index_file, 'r') as f:
                    index_data = json.load(f)

                # index.json에 있는 weight_map을 사용하여 각 파일에서 텐서 로드
                # weight_map은 각 텐서가 어느 파일에 있는지 매핑 정보를 포함
                if 'weight_map' in index_data:
                    # 파일 경로를 키로, 해당 파일에 속하는 텐서들의 리스트를 값으로 하는 딕셔너리
                    files_to_load = {}
                    for tensor_name, file_name in index_data['weight_map'].items():
                        if file_name not in files_to_load:
                            files_to_load[file_name] = []
                        files_to_load[file_name].append(tensor_name)

                    for file_name, tensor_names in files_to_load.items():
                        file_path = os.path.join(pretrained_path, file_name)
                        if not os.path.isfile(file_path):
                            print(f"Warning: Expected safetensors file {file_path} not found.")
                            continue

                        print(f"Loading part from {file_path}")
                        part_state_dict = load_file(file_path)
                        
                        for tensor_name in tensor_names:
                            if tensor_name in part_state_dict:
                                full_state_dict[tensor_name] = part_state_dict[tensor_name]
                            else:
                                print(f"Warning: Tensor {tensor_name} not found in {file_path}")

        if full_state_dict is not None:
            # kwargs에 full_state_dict를 추가하여 개별 load 함수로 전달합니다.
            kwargs['full_state_dict'] = full_state_dict
            
            # 각 컴포넌트 로드 함수를 호출합니다.
            model.load_llm(**kwargs)
            model.load_vision_tower(**kwargs)
            model.load_connector(**kwargs)
            
            print("All components (LLM, Vision Tower, Connector) loaded from model.safetensors successfully.")
        else:
            print(f"No model.safetensors found from provided config path. Proceeding with default loading behavior.")
            # model.safetensors가 없을 때, 기존 load_llm 등의 else 블록이 실행되도록 개별 호출합니다.
            # 이 kwargs에는 'full_state_dict'가 없으므로 기존 로딩 로직이 작동합니다.
            model.load_llm(**kwargs)
            model.load_vision_tower(**kwargs)
            model.load_connector(**kwargs)

        return model
            

        
        
